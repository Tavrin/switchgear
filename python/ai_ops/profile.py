from __future__ import annotations

import json
import os
from typing import Any

from .errors import Refuse
from .paths import reject_symlinks, require_absolute
from .schema import validate


def load_profile(path: str) -> dict[str, Any]:
    require_absolute(os.path.abspath(path), "profile")
    abs_path = reject_symlinks(os.path.abspath(path), "profile")
    if not os.path.isfile(abs_path):
        raise Refuse(f"missing profile {abs_path}")
    with open(abs_path, encoding="utf-8") as fh:
        data = json.load(fh)
    validate(data, "project-profile.schema.json")
    return data
