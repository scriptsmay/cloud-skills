#!/usr/bin/env python3
"""Upload an image to a self-hosted EasyImage 2.x instance and print its URL.

Stdlib only, no dependencies.

Configuration (environment variables):
    EASYIMAGE_BASE_URL   e.g. https://img.example.com   (no trailing path)
    EASYIMAGE_TOKEN      API token from the EasyImage admin panel
    EASYIMAGE_TOKEN_FILE path to a file containing only the token (optional,
                         used when EASYIMAGE_TOKEN is unset)

Usage:
    python3 easyimage_upload.py /path/to/image.png
    python3 easyimage_upload.py /path/to/image.png --json
    python3 easyimage_upload.py /path/to/image.png --base-url https://img.example.com

Output contract:
    - Success: the direct image URL on stdout (or sanitized JSON with --json).
      The `del` delete URL is credential-equivalent and is redacted unless
      --show-del is passed explicitly.
    - Failure: human-readable error on stderr, non-zero exit code.
    The token is never printed.
"""

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request

BOUNDARY = "----EasyImageBoundary7MA4YWxk"


def load_token(args) -> str:
    token = args.token or os.environ.get("EASYIMAGE_TOKEN", "")
    if not token:
        token_file = args.token_file or os.environ.get("EASYIMAGE_TOKEN_FILE", "")
        if token_file:
            with open(token_file, "r", encoding="utf-8") as fh:
                token = fh.read()
    # Token files commonly end with a newline; it must not leak into the form.
    return token.strip()


def build_body(file_path: str, token: str) -> bytes:
    with open(file_path, "rb") as fh:
        data = fh.read()
    filename = os.path.basename(file_path)
    ctype = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

    body = b""
    body += (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="token"\r\n\r\n'
        f"{token}\r\n"
    ).encode("utf-8")
    body += (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8")
    body += data
    body += f"\r\n--{BOUNDARY}--\r\n".encode("utf-8")
    return body


def upload(base_url: str, file_path: str, token: str, timeout: int) -> dict:
    url = base_url.rstrip("/") + "/api/index.php"
    req = urllib.request.Request(
        url,
        data=build_body(file_path, token),
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", help="image file to upload")
    parser.add_argument("--base-url", default=os.environ.get("EASYIMAGE_BASE_URL", ""),
                        help="EasyImage base URL (default: $EASYIMAGE_BASE_URL)")
    parser.add_argument("--token", default="",
                        help="API token (default: $EASYIMAGE_TOKEN or $EASYIMAGE_TOKEN_FILE)")
    parser.add_argument("--token-file", default="",
                        help="file containing the API token")
    parser.add_argument("--timeout", type=int, default=20, help="seconds (default: 20)")
    parser.add_argument("--json", action="store_true",
                        help="print the full response JSON instead of just the URL")
    parser.add_argument("--show-del", action="store_true",
                        help="include the sensitive delete URL in --json output")
    args = parser.parse_args(argv)

    if not args.base_url:
        print("error: no base URL — set EASYIMAGE_BASE_URL or pass --base-url",
              file=sys.stderr)
        return 2
    token = load_token(args)
    if not token:
        print("error: no token — set EASYIMAGE_TOKEN / EASYIMAGE_TOKEN_FILE "
              "or pass --token / --token-file", file=sys.stderr)
        return 2
    if not os.path.isfile(args.file):
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return 2
    if os.path.getsize(args.file) == 0:
        print(f"error: file is empty: {args.file}", file=sys.stderr)
        return 2

    try:
        result = upload(args.base_url, args.file, token, args.timeout)
    except urllib.error.URLError as exc:
        print(f"error: request failed: {exc.reason}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("error: server did not return JSON — check the base URL and "
              "that the path /api/index.php is correct", file=sys.stderr)
        return 1

    if result.get("result") != "success" or result.get("code") != 200:
        print(f"error: upload failed: code={result.get('code')} "
              f"message={result.get('message')}", file=sys.stderr)
        return 1

    if args.json:
        out = dict(result)
        if not args.show_del and "del" in out:
            out["del"] = "<redacted — pass --show-del to reveal>"
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(result["url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
