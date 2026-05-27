import runpod
from runpod.serverless.utils import rp_upload
import json
import urllib.request
import urllib.parse
import time
import os
import requests
import base64
from io import BytesIO
import websocket
import uuid
import tempfile
import socket
import traceback
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COMFY_API_AVAILABLE_INTERVAL_MS = int(os.environ.get("COMFY_API_AVAILABLE_INTERVAL_MS", 50))
COMFY_API_AVAILABLE_MAX_RETRIES = int(os.environ.get("COMFY_API_AVAILABLE_MAX_RETRIES", 0))
COMFY_API_FALLBACK_MAX_RETRIES = 500
COMFY_PID_FILE = "/tmp/comfyui.pid"
WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", 5))
WEBSOCKET_RECONNECT_DELAY_S = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", 3))

if os.environ.get("WEBSOCKET_TRACE", "false").lower() == "true":
    websocket.enableTrace(True)

COMFY_HOST = "127.0.0.1:8188"
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"


def _comfy_server_status():
    try:
        resp = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {"reachable": resp.status_code == 200, "status_code": resp.status_code}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _attempt_websocket_reconnect(ws_url, max_attempts, delay_s, initial_error):
    print(f"worker-comfyui - Websocket closed: {initial_error}. Reconnecting...")
    last_error = initial_error
    for attempt in range(max_attempts):
        srv_status = _comfy_server_status()
        if not srv_status["reachable"]:
            raise websocket.WebSocketConnectionClosedException("ComfyUI HTTP unreachable during reconnect")
        try:
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)
            print("worker-comfyui - Websocket reconnected.")
            return new_ws
        except (websocket.WebSocketException, ConnectionRefusedError, socket.timeout, OSError) as e:
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(delay_s)
    raise websocket.WebSocketConnectionClosedException(f"Reconnect failed. Last error: {last_error}")


def validate_input(job_input):
    if job_input is None:
        return None, "Please provide input"
    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"
    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"
    images = job_input.get("images")
    if images is not None:
        if not isinstance(images, list) or not all("name" in img and "image" in img for img in images):
            return None, "'images' must be a list of objects with 'name' and 'image' keys"
    return {"workflow": workflow, "images": images}, None


def _get_comfyui_pid():
    try:
        with open(COMFY_PID_FILE, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_comfyui_process_alive():
    pid = _get_comfyui_pid()
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def check_server(url, retries=0, delay=50):
    print(f"worker-comfyui - Checking API server at {url}...")
    delay = max(1, delay)
    log_every = max(1, int(10_000 / delay))
    attempt = 0
    while True:
        process_status = _is_comfyui_process_alive()
        if process_status is False:
            print("worker-comfyui - ComfyUI process has exited.")
            return False
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                print("worker-comfyui - API is reachable")
                return True
        except (requests.Timeout, requests.RequestException):
            pass
        attempt += 1
        fallback = retries if retries > 0 else COMFY_API_FALLBACK_MAX_RETRIES
        if process_status is None and attempt >= fallback:
            print(f"worker-comfyui - Failed to connect after {fallback} attempts.")
            return False
        if attempt % log_every == 0:
            print(f"worker-comfyui - Still waiting... ({(attempt * delay) / 1000:.0f}s elapsed)")
        time.sleep(delay / 1000)


def upload_images(images):
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}
    responses = []
    upload_errors = []
    print(f"worker-comfyui - Uploading {len(images)} image(s)...")
    for image in images:
        try:
            name = image["name"]
            image_data_uri = image["image"]
            base64_data = image_data_uri.split(",", 1)[1] if "," in image_data_uri else image_data_uri
            blob = base64.b64decode(base64_data)
            files = {"image": (name, BytesIO(blob), "image/png"), "overwrite": (None, "true")}
            response = requests.post(f"http://{COMFY_HOST}/upload/image", files=files, timeout=30)
            response.raise_for_status()
            responses.append(f"Uploaded {name}")
        except Exception as e:
            upload_errors.append(f"Error uploading {image.get('name', 'unknown')}: {e}")
    if upload_errors:
        return {"status": "error", "message": "Some images failed to upload", "details": upload_errors}
    return {"status": "success", "message": "All images uploaded successfully", "details": responses}


def queue_workflow(workflow, client_id):
    payload = {"prompt": workflow, "client_id": client_id}
    data = json.dumps(payload).encode("utf-8")
    response = requests.post(f"http://{COMFY_HOST}/prompt", data=data,
                             headers={"Content-Type": "application/json"}, timeout=30)
    if response.status_code == 400:
        raise ValueError(f"Workflow validation failed: {response.text}")
    response.raise_for_status()
    return response.json()


def get_history(prompt_id):
    response = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def get_file_data(filename, subfolder, file_type):
    data = {"filename": filename, "subfolder": subfolder, "type": file_type}
    url_values = urllib.parse.urlencode(data)
    try:
        response = requests.get(f"http://{COMFY_HOST}/view?{url_values}", timeout=120)
        response.raise_for_status()
        return response.content
    except Exception as e:
        print(f"worker-comfyui - Error fetching {filename}: {e}")
        return None


def handler(job):
    job_input = job["input"]
    job_id = job["id"]

    validated_data, error_message = validate_input(job_input)
    if error_message:
        return {"error": error_message}

    workflow = validated_data["workflow"]
    input_images = validated_data.get("images")

    if not check_server(f"http://{COMFY_HOST}/", COMFY_API_AVAILABLE_MAX_RETRIES, COMFY_API_AVAILABLE_INTERVAL_MS):
        return {"error": f"ComfyUI server ({COMFY_HOST}) not reachable."}

    if input_images:
        upload_result = upload_images(input_images)
        if upload_result["status"] == "error":
            return {"error": "Failed to upload input images", "details": upload_result["details"]}

    ws = None
    client_id = str(uuid.uuid4())
    prompt_id = None
    output_data = []
    errors = []

    try:
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)
        print("worker-comfyui - Websocket connected")

        queued = queue_workflow(workflow, client_id)
        prompt_id = queued.get("prompt_id")
        if not prompt_id:
            raise ValueError(f"Missing prompt_id in queue response: {queued}")
        print(f"worker-comfyui - Queued workflow: {prompt_id}")

        execution_done = False
        while True:
            try:
                out = ws.recv()
                if isinstance(out, str):
                    message = json.loads(out)
                    if message.get("type") == "executing":
                        data = message.get("data", {})
                        if data.get("node") is None and data.get("prompt_id") == prompt_id:
                            print(f"worker-comfyui - Execution finished: {prompt_id}")
                            execution_done = True
                            break
                    elif message.get("type") == "execution_error":
                        data = message.get("data", {})
                        if data.get("prompt_id") == prompt_id:
                            errors.append(f"Execution error: {data.get('exception_message')}")
                            break
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException as closed_err:
                ws = _attempt_websocket_reconnect(ws_url, WEBSOCKET_RECONNECT_ATTEMPTS, WEBSOCKET_RECONNECT_DELAY_S, closed_err)
                continue
            except json.JSONDecodeError:
                continue

        history = get_history(prompt_id)
        if prompt_id not in history:
            return {"error": f"Prompt {prompt_id} not found in history", "details": errors}

        outputs = history[prompt_id].get("outputs", {})
        print(f"worker-comfyui - Processing {len(outputs)} output nodes...")

        for node_id, node_output in outputs.items():
            # Handle images
            for image_info in node_output.get("images", []):
                filename = image_info.get("filename")
                if not filename or image_info.get("type") == "temp":
                    continue
                file_bytes = get_file_data(filename, image_info.get("subfolder", ""), image_info.get("type"))
                if file_bytes:
                    _process_output_file(job_id, filename, file_bytes, output_data, errors)

            # Handle videos
            for video_info in node_output.get("videos", []):
                filename = video_info.get("filename")
                if not filename or video_info.get("type") == "temp":
                    continue
                print(f"worker-comfyui - Fetching video: {filename}")
                file_bytes = get_file_data(filename, video_info.get("subfolder", ""), video_info.get("type"))
                if file_bytes:
                    _process_output_file(job_id, filename, file_bytes, output_data, errors)

    except websocket.WebSocketException as e:
        return {"error": f"WebSocket error: {e}"}
    except requests.RequestException as e:
        return {"error": f"HTTP error: {e}"}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        print(traceback.format_exc())
        return {"error": f"Unexpected error: {e}"}
    finally:
        if ws and ws.connected:
            ws.close()

    if not output_data and errors:
        return {"error": "Job failed", "details": errors}

    result = {}
    if output_data:
        result["outputs"] = output_data
    if errors:
        result["errors"] = errors
    if not output_data and not errors:
        result["status"] = "success_no_output"
        result["outputs"] = []

    print(f"worker-comfyui - Done. {len(output_data)} output(s).")
    return result


def _process_output_file(job_id, filename, file_bytes, output_data, errors):
    ext = os.path.splitext(filename)[1] or ".bin"
    if os.environ.get("BUCKET_ENDPOINT_URL"):
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name
            url = rp_upload.upload_image(job_id, tmp_path)
            os.remove(tmp_path)
            output_data.append({"filename": filename, "type": "s3_url", "data": url})
            print(f"worker-comfyui - Uploaded {filename} to S3: {url}")
        except Exception as e:
            errors.append(f"S3 upload error for {filename}: {e}")
    else:
        output_data.append({
            "filename": filename,
            "type": "base64",
            "data": base64.b64encode(file_bytes).decode("utf-8"),
        })


if __name__ == "__main__":
    print("worker-comfyui - Starting handler...")
    runpod.serverless.start({"handler": handler})
