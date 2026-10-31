"""
Exports an SSCD copy detection model (https://github.com/facebookresearch/sscd-copy-detection, MIT license)
from the official TorchScript release to ONNX, for the opt-in crop detection stage of the experimental
matching pipeline (py_image_dedup/matching/embeddings.py, see docs/matching-prototype.md).

Needs PyTorch, onnx and onnxruntime, which are NOT dependencies of py-image-dedup. Run it in a throw-away
virtualenv, e.g.:

    uv venv /tmp/sscd-export && VIRTUAL_ENV=/tmp/sscd-export uv pip install \\
        --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple \\
        torch onnx onnxscript onnxruntime numpy
    /tmp/sscd-export/bin/python tools/export_sscd_onnx.py --output ~/.cache/py-image-dedup/models/sscd_disc_mixup.onnx

The TorchScript file is downloaded from Meta's release URL and its SHA-256 is verified. The export has a dynamic
batch size and image size (input "images": float32 [N, 3, H, W], ImageNet normalised; output "embeddings":
float32 [N, 512], L2 normalised). The script checks that onnxruntime and PyTorch produce the same embeddings and
prints the SHA-256 of the result, which can be passed to the pipeline as the expected model checksum.
"""
import argparse
import hashlib
import sys
import tempfile
import urllib.request
from pathlib import Path

MODELS = {
    # name: (url, sha256)
    "sscd_disc_mixup": (
        "https://dl.fbaipublicfiles.com/sscd-copy-detection/sscd_disc_mixup.torchscript.pt",
        "9f26bd4c848cc19b73d2ae92eea6e04886f61a7b764ceb7a13aeee62e6a6db56",
    ),
}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path, expected_sha256: str):
    print(f"Downloading {url}")
    urllib.request.urlretrieve(url, target)
    actual = sha256_of(target)
    if actual != expected_sha256:
        raise SystemExit(f"Checksum mismatch for {url}: expected {expected_sha256}, got {actual}")
    print(f"  sha256 {actual}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=sorted(MODELS), default="sscd_disc_mixup")
    parser.add_argument("--torchscript", type=Path, default=None,
                        help="use an already downloaded TorchScript file instead of downloading it")
    parser.add_argument("--output", type=Path, required=True, help="target .onnx file")
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    import numpy as np
    import onnxruntime
    import torch

    url, expected_sha256 = MODELS[args.model]
    with tempfile.TemporaryDirectory() as temp_dir:
        torchscript = args.torchscript
        if torchscript is None:
            torchscript = Path(temp_dir) / f"{args.model}.torchscript.pt"
            download(url, torchscript, expected_sha256)
        elif sha256_of(torchscript) != expected_sha256:
            raise SystemExit(f"Checksum mismatch for {torchscript}")

        model = torch.jit.load(str(torchscript), map_location="cpu").eval()
        example = torch.randn(1, 3, 320, 320)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with torch.no_grad():
            torch.onnx.export(
                model, (example,), str(args.output),
                input_names=["images"], output_names=["embeddings"],
                dynamic_axes={"images": {0: "batch", 2: "height", 3: "width"}, "embeddings": {0: "batch"}},
                opset_version=args.opset, dynamo=False,
            )

        # verify: same embeddings from onnxruntime and PyTorch, also for another input size
        session = onnxruntime.InferenceSession(str(args.output), providers=["CPUExecutionProvider"])
        for shape in ((2, 3, 320, 320), (1, 3, 288, 384)):
            sample = torch.randn(*shape)
            with torch.no_grad():
                expected = model(sample).numpy()
            actual = session.run(["embeddings"], {"images": sample.numpy()})[0]
            difference = float(np.abs(expected - actual).max())
            print(f"  input {shape}: max abs difference torch/onnxruntime {difference:.2e}")
            if difference > 1e-3:
                raise SystemExit("onnxruntime output differs from PyTorch")

    print(f"Wrote {args.output} ({args.output.stat().st_size / 1e6:.1f} MB)")
    print(f"  sha256 {sha256_of(args.output)}")


if __name__ == "__main__":
    sys.exit(main())
