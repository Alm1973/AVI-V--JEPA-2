$py = "$env:USERPROFILE\avi\.venv-yolo\Scripts\python.exe"
& $py -c @"
import torch
free, total = torch.cuda.mem_get_info()
print(f'free {free/1e9:.2f} GB of {total/1e9:.2f} GB')
print(f'V-JEPA 2 ViT-L needs about 0.74 GB -> {\"fits\" if free/1e9 > 1.3 else \"TIGHT/NO\"}')
"@ 2>&1
