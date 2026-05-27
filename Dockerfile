FROM runpod/worker-comfyui:5.8.5-base

# Custom nodes
RUN git clone https://github.com/tenitsky/tenitsky-prompt-cycler-simple /comfyui/custom_nodes/tenitsky-prompt-cycler-simple

RUN git clone https://github.com/Lightricks/ComfyUI-LTXVideo /comfyui/custom_nodes/ComfyUI-LTXVideo \
  && pip install -r /comfyui/custom_nodes/ComfyUI-LTXVideo/requirements.txt

RUN git clone https://github.com/evanspearman/ComfyMath /comfyui/custom_nodes/ComfyMath

# Example input image
RUN wget --progress=dot:giga -O '/comfyui/input/example.png' \
  "https://cool-anteater-319.convex.cloud/api/storage/21936494-eafb-4f83-a21b-6c1e65e234a8"

# Handler and startup script
COPY handler.py /handler.py
COPY start.sh /my-start.sh
RUN chmod +x /my-start.sh

CMD ["/my-start.sh"]
