import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
from typing import Any

import frontmatter
import requests
from dotenv import load_dotenv

load_dotenv()

SHORTCUT_TOKEN = os.getenv("SHORTCUT_TOKEN")
DOCS_DIR = "./.docs"
API_BASE_URL = "https://api.app.shortcut.com/api/v3/documents"
FILES_API_URL = "https://api.app.shortcut.com/api/v3/files"

auth_headers = {"Shortcut-Token": SHORTCUT_TOKEN}
json_headers = {**auth_headers, "Content-Type": "application/json"}


# ──────────────────────────────────────────────
# Front matter
# ──────────────────────────────────────────────

DOC_METADATA_FIELDS = ("id", "app_url", "archived", "created_at", "updated_at")
LOCAL_METADATA_FIELDS = ("hash",)
SYNC_NOTE = "DO NOT CHANGE - metadata below is managed by Shortcut"


def parse_md_file(file_path: str) -> tuple[dict, str]:
    post = frontmatter.load(file_path)
    raw_metadata = dict(post.metadata)
    nested_metadata = raw_metadata.get("metadata")

    metadata: dict[str, str] = {}
    for key, value in raw_metadata.items():
        if key == "metadata":
            continue
        metadata[str(key)] = "" if value is None else str(value)

    if isinstance(nested_metadata, dict):
        for key, value in nested_metadata.items():
            metadata[str(key)] = "" if value is None else str(value)

    return metadata, post.content.strip()


def load_md_post(file_path: str) -> frontmatter.Post:
    return frontmatter.load(file_path)


def format_front_matter_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'

    escaped = str(value).replace('"', '\\"')
    return f'"{escaped}"'


def build_hash_payload_from_state(
    metadata: dict[str, Any], body: str
) -> dict[str, Any]:
    return {"content": body.strip()}


def compute_file_hash(file_path: str) -> str:
    post = load_md_post(file_path)
    payload = {"content": post.content.strip()}
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.blake2b(serialized.encode("utf-8"), digest_size=8).hexdigest()


def compute_state_hash(metadata: dict[str, Any], body: str) -> str:
    payload = build_hash_payload_from_state(metadata, body)
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.blake2b(serialized.encode("utf-8"), digest_size=8).hexdigest()


def update_front_matter(file_path: str, metadata: dict[str, Any], body: str) -> None:
    lines = ["---"]
    title = metadata.get("title", "<CHANGE_ME>")
    lines.append(f"title: {format_front_matter_value(title)}")

    metadata_lines: list[str] = []
    for field in (*DOC_METADATA_FIELDS, *LOCAL_METADATA_FIELDS):
        if field not in metadata:
            continue

        metadata_lines.append(
            f"  {field}: {format_front_matter_value(metadata[field])}"
        )

    if metadata_lines:
        lines.append(f"note: {format_front_matter_value(SYNC_NOTE)}")
        lines.append("metadata:")
        lines.extend(metadata_lines)

    lines.append("---")
    new_content = "\n".join(lines) + f"\n\n{body}"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)


def slugify_filename(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "new-doc"


def build_doc_template(title: str = "<CHANGE_ME>") -> str:
    return f'---\ntitle: "{title}"\n---\n'


def create_new_doc(
    title: str = "<CHANGE_ME>", output_path: str | None = None, force: bool = False
) -> None:
    if output_path:
        file_path = output_path
    else:
        file_path = os.path.join(DOCS_DIR, f"{slugify_filename(title)}.md")

    abs_path = os.path.abspath(file_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    if os.path.exists(abs_path) and not force:
        print(f"File already exists: {abs_path}")
        print("Use --force to overwrite the file.")
        return

    content = build_doc_template(title)
    with open(abs_path, "w", encoding="utf-8") as file:
        file.write(content)

    print(f"Created new doc template: {abs_path}")


def ensure_shortcut_token() -> None:
    if not SHORTCUT_TOKEN:
        raise RuntimeError("Missing SHORTCUT_TOKEN in the environment.")


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
    timeout: int = 30,
) -> requests.Response:
    ensure_shortcut_token()
    response = requests.request(
        method,
        url,
        headers=headers or auth_headers,
        params=params,
        json=json,
        timeout=timeout,
    )
    response.raise_for_status()
    return response


# ──────────────────────────────────────────────
# Upload a file to Shortcut and return its public URL
# ──────────────────────────────────────────────


def upload_file_to_shortcut(file_path: str, filename: str) -> str | None:
    """Upload a file to the Shortcut Files API and return its public URL."""
    try:
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(file_path, "rb") as f:
            response = requests.post(
                FILES_API_URL,
                headers=auth_headers,
                files={"file": (filename, f, mime_type)},
                timeout=30,
            )
        if response.status_code == 201:
            data = response.json()
            # Shortcut returns a list for file uploads.
            if isinstance(data, list):
                data = data[0]
            url = data.get("url")
            print(f"  Uploaded: {filename} -> {url}")
            return url
        else:
            print(
                f"  Upload failed ({filename}) [{response.status_code}]: {response.text}"
            )
            return None
    except Exception as e:
        print(f"  Upload error for {filename}: {e}")
        return None


# ──────────────────────────────────────────────
# Markdown segment splitter (code-fence aware)
# ──────────────────────────────────────────────

# Matches any fenced code block (``` or ~~~), preserving the full block including fence lines.
# Uses MULTILINE so ^ and $ match line boundaries, and DOTALL so .* spans newlines.
_CODE_FENCE_RE = re.compile(
    r"(^(?:```|~~~)[^\S\r\n]*\S*[^\S\r\n]*\r?\n.*?\r?\n(?:```|~~~)[^\S\r\n]*$)",
    re.DOTALL | re.MULTILINE,
)


def split_markdown(md: str) -> list[tuple[str, bool]]:
    """
    Split markdown into a list of (segment, is_code_block) tuples.

    Segments alternate between non-code prose and fenced code blocks so
    callers can skip processing the interiors of code blocks.
    """
    parts = _CODE_FENCE_RE.split(md)
    result: list[tuple[str, bool]] = []
    for part in parts:
        if not part:
            continue
        is_code = bool(_CODE_FENCE_RE.fullmatch(part))
        result.append((part, is_code))
    return result


# ──────────────────────────────────────────────
# Mermaid -> PNG -> upload -> replace with URL
# ──────────────────────────────────────────────

# Anchored to line boundaries so the closing ``` cannot accidentally match
# the opening fence of the very next code block.
_MERMAID_RE = re.compile(
    r"^```mermaid[^\S\r\n]*\r?\n(.*?)\r?\n^```[^\S\r\n]*$",
    re.DOTALL | re.MULTILINE,
)


def render_mermaid_to_png(mermaid_code: str) -> str | None:
    """
    Use mmdc to render Mermaid code into a temporary PNG file.
    Install with: npm i -g @mermaid-js/mermaid-cli
    """
    src_file = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False) as src:
            src.write(mermaid_code)
            src_file = src.name

        output_file = src_file.removesuffix(".mmd") + ".png"

        result = subprocess.run(
            [
                "mmdc",
                "-i",
                src_file,
                "-o",
                output_file,
                "--quiet",
                "--backgroundColor",
                "white",
            ],
            capture_output=True,
            timeout=20,
        )

        if result.returncode == 0:
            return output_file

        stderr = result.stderr.decode().strip()
        print(f"  mmdc failed to render PNG: {stderr or 'unknown error'}")

        if os.path.exists(output_file):
            os.unlink(output_file)

        return None

    except FileNotFoundError:
        print("  mmdc was not found. Install it with: npm i -g @mermaid-js/mermaid-cli")
        return None
    except Exception as e:
        print(f"  Mermaid render error: {e}")
        return None
    finally:
        if src_file and os.path.exists(src_file):
            os.unlink(src_file)


def process_mermaid_blocks(md_body: str) -> str:
    """
    Find all ```mermaid ... ``` blocks:
      1. Render to PNG
      2. Upload the PNG to Shortcut
      3. Replace the block with ![...](url)
    If any step fails, keep the original block unchanged.

    Uses a multiline-anchored regex so that the closing fence is always
    matched at the start of a line and cannot overlap with the opening
    fence of the immediately following code block.
    """
    counter = [0]  # Use a list so the nested function can mutate the counter.

    def replace_block(match: re.Match) -> str:
        code = match.group(1).strip()
        counter[0] += 1
        label = f"diagram-{counter[0]}"

        print(f"  Rendering Mermaid block #{counter[0]}...")
        png_path = render_mermaid_to_png(code)
        if not png_path:
            return match.group(0)

        try:
            url = upload_file_to_shortcut(png_path, f"{label}.png")
        finally:
            if os.path.exists(png_path):
                os.unlink(png_path)

        if url:
            return f"![{label}]({url})"
        return match.group(0)

    return _MERMAID_RE.sub(replace_block, md_body)


# ──────────────────────────────────────────────
# Local images -> upload -> replace with URL
# ──────────────────────────────────────────────

_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def process_local_images(md_body: str, md_file_dir: str) -> str:
    """
    Find ![alt](./relative/path) or ![alt](filename.jpg) references that
    appear **outside** of fenced code blocks:
      1. Resolve the path relative to the Markdown file
      2. Upload it to Shortcut
      3. Replace it with the uploaded URL
    Skip sources that are already remote http/https URLs.
    References inside code blocks are left untouched to avoid mangling
    example content such as JSON or shell snippets.
    """

    def replace_image(match: re.Match) -> str:
        alt_text = match.group(1)
        src = match.group(2)

        # Skip sources that already point to a remote URL.
        if src.startswith("http://") or src.startswith("https://"):
            return match.group(0)

        # Resolve the path relative to the Markdown file location.
        abs_path = os.path.normpath(os.path.join(md_file_dir, src))

        if not os.path.isfile(abs_path):
            print(f"  Image not found: {abs_path}")
            return match.group(0)

        filename = os.path.basename(abs_path)
        print(f"  Uploading image: {filename}...")
        url = upload_file_to_shortcut(abs_path, filename)

        if url:
            return f"![{alt_text}]({url})"
        return match.group(0)

    # Process segment-by-segment so that image refs inside code blocks are
    # never touched, preventing accidental corruption of code examples.
    segments = split_markdown(md_body)
    result: list[str] = []
    for segment, is_code in segments:
        if is_code:
            result.append(segment)
        else:
            result.append(_IMAGE_RE.sub(replace_image, segment))
    return "".join(result)


# ──────────────────────────────────────────────
# Sync
# ──────────────────────────────────────────────


def iter_markdown_files() -> list[str]:
    markdown_files: list[str] = []
    for root, _, files in os.walk(DOCS_DIR):
        for file in sorted(files):
            if file.endswith(".md"):
                markdown_files.append(os.path.join(root, file))
    return markdown_files


def sync_docs(dry_run: bool = False) -> None:
    for file_path in iter_markdown_files():
        file = os.path.basename(file_path)
        md_file_dir = os.path.dirname(os.path.abspath(file_path))

        try:
            metadata, body = parse_md_file(file_path)
        except Exception as e:
            print(f"Could not read {file_path}: {e}")
            continue

        title = metadata.get("title", file.removesuffix(".md"))
        doc_id = (
            metadata.get("id")
            or metadata.get("doc_id")
            or metadata.get("shortcut_doc_id")
        )
        stored_hash = metadata.get("hash", "")
        current_hash = compute_file_hash(file_path)

        print(f"\nProcessing: {title}")

        if dry_run:
            if doc_id and stored_hash and stored_hash == current_hash:
                print(f"  [dry-run] SKIP: {title} (hash unchanged)")
                continue

            action = "UPDATE" if doc_id else "CREATE"
            print(f"  [dry-run] {action}: {title}")
            continue

        if doc_id and stored_hash and stored_hash == current_hash:
            print(f"  Skipped: {title} (hash unchanged)")
            continue

        # Process Mermaid blocks and local image references before syncing.
        processed_body = process_mermaid_blocks(body)
        processed_body = process_local_images(processed_body, md_file_dir)

        payload = {
            "title": title,
            "content": processed_body,
            "content_format": "markdown",
        }

        try:
            if doc_id:
                url = f"{API_BASE_URL}/{doc_id}"
                response = requests.put(
                    url, json=payload, headers=json_headers, timeout=10
                )
                if response.status_code == 200:
                    print(f"  Updated: {title}")
                    doc_metadata = response.json()
                    doc_metadata["hash"] = compute_state_hash(doc_metadata, body)
                    update_front_matter(file_path, doc_metadata, body)
                else:
                    print(f"  Update failed [{response.status_code}]: {response.text}")
            else:
                response = requests.post(
                    API_BASE_URL, json=payload, headers=json_headers, timeout=10
                )
                if response.status_code == 201:
                    new_doc = response.json()
                    new_id = new_doc.get("id")
                    print(f"  Created: {title} -> ID: {new_id}")
                    full_doc = get_shortcut_doc(new_id)
                    full_doc["hash"] = compute_state_hash(full_doc, body)
                    update_front_matter(file_path, full_doc, body)
                else:
                    print(f"  Create failed [{response.status_code}]: {response.text}")
        except requests.RequestException as e:
            print(f"  Network error: {e}")


def list_shortcut_docs() -> list[dict[str, Any]]:
    response = request_json("GET", API_BASE_URL, headers=json_headers, timeout=30)
    return response.json()


def get_shortcut_doc(doc_id: str) -> dict[str, Any]:
    response = request_json(
        "GET",
        f"{API_BASE_URL}/{doc_id}",
        headers=json_headers,
        params={"content_format": "html"},
        timeout=30,
    )
    return response.json()


def list_shortcut_files() -> list[dict[str, Any]]:
    response = request_json("GET", FILES_API_URL, headers=json_headers, timeout=30)
    return response.json()


def delete_shortcut_file(file_id: int) -> None:
    request_json(
        "DELETE", f"{FILES_API_URL}/{file_id}", headers=json_headers, timeout=30
    )


def collect_referenced_file_urls_from_docs() -> set[str]:
    referenced_urls: set[str] = set()
    docs = list_shortcut_docs()

    for doc in docs:
        doc_id = doc["id"]
        title = doc.get("title", doc_id)
        print(f"Scanning doc: {title}")

        try:
            full_doc = get_shortcut_doc(doc_id)
        except requests.RequestException as e:
            print(f"  Could not read doc {doc_id}: {e}")
            continue

        for key in ("content_markdown", "content_html", "content"):
            content = full_doc.get(key)
            if not content:
                continue

            for url in re.findall(
                r"https://media\.app\.shortcut\.com/[^\s)'\"<>]+", content
            ):
                referenced_urls.add(url)

    return referenced_urls


def clean_unreferenced_files(dry_run: bool = False) -> None:
    print("Collecting file references from Shortcut Docs...")
    referenced_urls = collect_referenced_file_urls_from_docs()

    print("Loading uploaded files from Shortcut...")
    files = list_shortcut_files()

    unreferenced_files: list[dict[str, Any]] = []
    for file in files:
        file_url = file.get("url")
        story_ids = file.get("story_ids") or []

        if not file_url:
            continue

        if file_url in referenced_urls:
            continue

        # Keep files that are attached to stories to avoid deleting non-Docs attachments.
        if story_ids:
            continue

        unreferenced_files.append(file)

    if not unreferenced_files:
        print("No unreferenced files found.")
        return

    print(f"Found {len(unreferenced_files)} unreferenced file(s).")
    for file in unreferenced_files:
        file_id = file["id"]
        filename = file.get("filename") or file.get("name") or str(file_id)
        file_url = file.get("url", "")

        if dry_run:
            print(f"  [dry-run] DELETE file {file_id}: {filename} → {file_url}")
            continue

        try:
            delete_shortcut_file(file_id)
            print(f"  Deleted file {file_id}: {filename}")
        except requests.RequestException as e:
            print(f"  Failed to delete file {file_id}: {e}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Shortcut tools for Docs and Files")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser(
        "create-new-doc", help="Create a new local doc from the default template"
    )
    create_parser.add_argument(
        "title", nargs="?", default="<CHANGE_ME>", help="Title to use for the new doc"
    )
    create_parser.add_argument(
        "--output", help="Optional output path for the new Markdown file"
    )
    create_parser.add_argument(
        "--force", action="store_true", help="Overwrite the file if it already exists"
    )

    sync_parser = subparsers.add_parser(
        "sync-docs", help="Sync local markdown files to Shortcut Docs"
    )
    sync_parser.add_argument(
        "--dry-run", action="store_true", help="Preview changes without calling the API"
    )

    clean_parser = subparsers.add_parser(
        "clean-unreferenced-files",
        aliases=[
            "clear-unreferenced-files",
            "clear-unrefferenced-files",
            "clean-files",
        ],
        help="Delete UploadedFiles that are no longer referenced by Shortcut Docs",
    )
    clean_parser.add_argument(
        "--dry-run", action="store_true", help="Only print files that would be deleted"
    )

    args = parser.parse_args()

    if args.command == "create-new-doc":
        create_new_doc(title=args.title, output_path=args.output, force=args.force)
    elif args.command == "sync-docs":
        sync_docs(dry_run=args.dry_run)
    elif args.command == "clean-unreferenced-files":
        clean_unreferenced_files(dry_run=args.dry_run)
