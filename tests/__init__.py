
from __future__ import annotations

import base64


# Real 1x1 PNG used by upload tests. Security validation deliberately rejects
# signature-only byte strings that are not decodable images.
VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
