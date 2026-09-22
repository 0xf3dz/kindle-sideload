#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
CALIBRE_VERSION = "8.5.0"
DEFAULT_CONVERTER = ROOT / ".tools" / f"calibre-{CALIBRE_VERSION}" / "calibre.app" / "Contents" / "MacOS" / "ebook-convert"
STATIC_FILE = ROOT / "static" / "index.html"
OUTPUT_DIR = ROOT / "output"
INDEX_FILE = OUTPUT_DIR / "imports.json"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
CONVERSION_LOCK = threading.Lock()
IMPORTS_LOCK = threading.Lock()


def converter_path() -> Path:
    override = os.environ.get("EBOOK_CONVERT")
    return Path(override).expanduser().resolve() if override else DEFAULT_CONVERTER


def calibre_version(binary: Path) -> str:
    result = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        check=True,
        text=True,
        timeout=15,
    )
    return result.stdout.strip()


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def safe_stem(filename: str) -> str:
    stem = Path(filename).stem.strip()
    stem = re.sub(r"[^\w .,'()&-]+", "", stem, flags=re.UNICODE)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem[:120] or "book"


def validate_epub(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise ValueError("The file is not a valid EPUB archive.")
    with zipfile.ZipFile(path) as archive:
        if "META-INF/container.xml" not in archive.namelist():
            raise ValueError("The EPUB does not contain META-INF/container.xml.")

def _read_imports_unlocked() -> list[dict[str, object]]:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def list_imports() -> list[dict[str, object]]:
    with IMPORTS_LOCK:
        return [
            record
            for record in _read_imports_unlocked()
            if isinstance(record, dict)
            and isinstance(record.get("id"), str)
            and (OUTPUT_DIR / f"{record['id']}.azw3").is_file()
        ]


def record_import(record: dict[str, object]) -> None:
    with IMPORTS_LOCK:
        records = _read_imports_unlocked()
        records.insert(0, record)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        temporary = INDEX_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, indent=2), encoding="utf-8")
        temporary.replace(INDEX_FILE)


class ConverterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], public_host: str, binary: Path):
        super().__init__(address, handler)
        self.public_host = public_host
        self.binary = binary
        self.engine_version = calibre_version(binary)

    @property
    def public_origin(self) -> str:
        return f"http://{self.public_host}:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server: ConverterServer

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.client_address[0]} - {format % args}")

    def send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            body = STATIC_FILE.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/info":
            self.send_json(
                HTTPStatus.OK,
                {
                    "engine": self.server.engine_version,
                    "kindle_origin": self.server.public_origin,
                    "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
                },
            )
            return
        if path == "/api/imports":
            imports = []
            for record in list_imports():
                book_id = str(record["id"])
                relative_url = f"/b/{book_id}.azw3"
                imports.append(
                    {
                        **record,
                        "download_url": relative_url,
                        "kindle_url": f"{self.server.public_origin}{relative_url}",
                    }
                )
            self.send_json(HTTPStatus.OK, {"imports": imports})
            return


        match = re.fullmatch(r"/b/([a-f0-9]{8})\.azw3", path)
        if match:
            output = OUTPUT_DIR / f"{match.group(1)}.azw3"
            if output.is_file():
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(output.stat().st_size))
                self.send_header("Content-Disposition", f'attachment; filename="{output.name}"')
                self.end_headers()
                with output.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
                return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        request = urlsplit(self.path)
        if request.path != "/api/convert":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        filename = unquote(parse_qs(request.query).get("filename", [""])[0])
        if Path(filename).suffix.lower() != ".epub":
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Select an EPUB file."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": f"The EPUB must be between 1 byte and {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."},
            )
            return

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        book_id = uuid.uuid4().hex[:8]
        output = OUTPUT_DIR / f"{book_id}.azw3"

        try:
            with tempfile.TemporaryDirectory(prefix="epub-to-azw3-") as temp_name:
                source = Path(temp_name) / f"{safe_stem(filename)}.epub"
                remaining = content_length
                with source.open("wb") as target:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("The upload ended before the complete EPUB arrived.")
                        target.write(chunk)
                        remaining -= len(chunk)

                validate_epub(source)
                with CONVERSION_LOCK:
                    result = subprocess.run(
                        [str(self.server.binary), str(source), str(output)],
                        capture_output=True,
                        text=True,
                        timeout=1800,
                    )
                if result.returncode != 0 or not output.is_file():
                    output.unlink(missing_ok=True)
                    detail = (result.stderr or result.stdout).strip().splitlines()
                    message = detail[-1] if detail else "calibre did not create an AZW3 file."
                    raise RuntimeError(message)
        except ValueError as error:
            output.unlink(missing_ok=True)
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except subprocess.TimeoutExpired:
            output.unlink(missing_ok=True)
            self.send_json(HTTPStatus.GATEWAY_TIMEOUT, {"error": "Conversion exceeded 30 minutes."})
            return
        except Exception as error:
            output.unlink(missing_ok=True)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            return
        record = {
            "id": book_id,
            "name": f"{safe_stem(filename)}.azw3",
            "size": output.stat().st_size,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        record_import(record)


        relative_url = f"/b/{book_id}.azw3"
        self.send_json(
            HTTPStatus.OK,
            {
                **record,
                "download_url": relative_url,
                "kindle_url": f"{self.server.public_origin}{relative_url}",
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert EPUB files to AZW3 with calibre 8.5 and serve them on the local network.")
    parser.add_argument("--host", default="0.0.0.0", help="Listen address. Default: 0.0.0.0")
    parser.add_argument("--port", type=int, default=8787, help="Listen port. Default: 8787")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser.")
    args = parser.parse_args()

    binary = converter_path()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit("calibre 8.5 is not installed. Run ./setup first.")

    public_host = local_ip()
    server = ConverterServer((args.host, args.port), Handler, public_host, binary)
    local_url = f"http://127.0.0.1:{server.server_port}"
    print(f"Engine: {server.engine_version}")
    print(f"Mac:    {local_url}")
    print(f"Kindle: {server.public_origin}")
    print("Press Control-C to stop.")

    if not args.no_open:
        import webbrowser

        threading.Timer(0.5, lambda: webbrowser.open(local_url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
