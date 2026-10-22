"""No chat-role or generation tokens may leak into page-image embeddings."""
from pathlib import Path

from jinja2 import Template


def test_native_image_placeholder_only():
    path = Path(__file__).resolve().parents[3] / "deploy/shared-embedding/image-template.jinja"
    template = Template(path.read_text())
    assert template.render(messages=[{"role": "user", "content": [
        {"type": "image", "image_url": {"url": "data:image/png;base64,test"}},
    ]}]) == "<|image|>"
