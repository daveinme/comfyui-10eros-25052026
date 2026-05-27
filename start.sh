#!/bin/bash
set -e

VOLUME_DIR="/runpod-volume"
MODELS_DIR="$VOLUME_DIR/models"

# Point ComfyUI to models on the network volume
cat > /comfyui/extra_model_paths.yaml <<EOF
runpod_worker_comfy:
  base_path: $VOLUME_DIR
  checkpoints: models/checkpoints/
  text_encoders: models/text_encoders/
  loras: models/loras/
  latent_upscale_models: models/latent_upscale_models/
  vae: models/vae/
  clip: models/clip/
  unet: models/unet/
EOF

echo "Starting ComfyUI worker..."
exec /start.sh
