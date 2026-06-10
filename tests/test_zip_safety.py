import base64
import io
import zipfile

import pytest

from .conftest import ToolFailure, make_config, make_mcp, call_tool


def zip_b64(entries: dict[str, bytes], infos: list[zipfile.ZipInfo] | None = None) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for info in infos or []:
            zf.writestr(info, b"target")
    return base64.b64encode(buf.getvalue()).decode()


async def upload_zip(call, content_b64: str):
    return await call(
        "artifact_upload", filename="evil.zip", content=content_b64, encoding="base64"
    )


async def test_zip_slip_rejected(call):
    with pytest.raises(ToolFailure) as exc:
        await upload_zip(call, zip_b64({"../../etc/cron.d/evil": b"x"}))
    assert exc.value.code == "ZIP_UNSAFE"

    with pytest.raises(ToolFailure) as exc:
        await upload_zip(call, zip_b64({"ok/../../escape.txt": b"x"}))
    assert exc.value.code == "ZIP_UNSAFE"


async def test_absolute_path_rejected(call):
    with pytest.raises(ToolFailure) as exc:
        await upload_zip(call, zip_b64({"/etc/passwd": b"x"}))
    assert exc.value.code == "ZIP_UNSAFE"

    with pytest.raises(ToolFailure) as exc:
        await upload_zip(call, zip_b64({"C:\\windows\\evil.txt": b"x"}))
    assert exc.value.code == "ZIP_UNSAFE"


async def test_symlink_rejected(call):
    info = zipfile.ZipInfo("innocent-link")
    info.external_attr = (0o120777 << 16)  # symlink mode in the unix attr bits
    with pytest.raises(ToolFailure) as exc:
        await upload_zip(call, zip_b64({"readme.txt": b"hi"}, infos=[info]))
    assert exc.value.code == "ZIP_UNSAFE"


async def test_oversized_decompression_rejected(tmp_path):
    # max_upload_mb=1 → decompressed cap is 4 MB; 5 MB of zeros compresses tiny
    mcp = make_mcp(make_config(tmp_path, max_upload_mb=1))
    bomb = zip_b64({"zeros.bin": b"\x00" * (5 * 1024 * 1024)})
    with pytest.raises(ToolFailure) as exc:
        await call_tool(
            mcp, "artifact_upload", filename="bomb.zip", content=bomb, encoding="base64"
        )
    assert exc.value.code == "ZIP_UNSAFE"


async def test_too_many_entries_rejected(call):
    entries = {f"f/{i}.txt": b"" for i in range(2001)}
    with pytest.raises(ToolFailure) as exc:
        await upload_zip(call, zip_b64(entries))
    assert exc.value.code == "ZIP_UNSAFE"


async def test_safe_plain_zip_accepted(call):
    result = await upload_zip(call, zip_b64({"docs/readme.md": b"# hi"}))
    assert result["mime"] == "application/zip"
    assert result["debug_capture"] is None
