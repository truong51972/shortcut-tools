# Shortcut Tools

A collection of command-line tools to streamline managing Shortcut Docs and uploaded files.

## Features

- **Create New Docs**: Quickly scaffold a new local Markdown document from a template.
- **Sync Docs**: Sync local Markdown files to Shortcut Docs.
  - Automatically uploads local images and replaces paths with remote URLs.
  - Renders `mermaid` diagrams to PNG, uploads them, and replaces the code block with the image URL.
- **Clean Unreferenced Files**: Find and delete uploaded files that are no longer referenced in any Shortcut Doc, helping to keep your file storage clean.

## Setup

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
    *Note: You may need to create a `requirements.txt` file based on the imports in `src/shortcut_tools/main.py`.*

2.  **Install Mermaid CLI**: For Mermaid diagram rendering, you need to install `@mermaid-js/mermaid-cli`:
    ```bash
    npm i -g @mermaid-js/mermaid-cli
    ```

3.  **Environment Variables**:
    Create a `.env` file in the project root and add your Shortcut API token:
    ```
    SHORTCUT_TOKEN="your_shortcut_api_token_here"
    ```

## Usage

The main script is located at `src/shortcut_tools/main.py`. You can run it from the project root directory.

### Create a New Document

Create a new Markdown file in the `./.docs` directory.

```bash
python src/shortcut_tools/main.py create-new-doc "My New Document Title"
```

**Arguments**:
- `title` (optional): The title for the new document. Defaults to `<CHANGE_ME>`.
- `--output` (optional): Specify a custom output path for the file.
- `--force` (optional): Overwrite the file if it already exists.

### Sync Documents

Sync all local `.md` files from the `./.docs` directory to Shortcut.

```bash
python src/shortcut_tools/main.py sync-docs
```

**Options**:
- `--dry-run`: Preview the changes that would be made without actually calling the Shortcut API. The script will print whether each document would be created, updated, or skipped.

**Syncing Process**:
1.  The script iterates through all `.md` files in the `./.docs` directory.
2.  It computes a hash of the file's content. If the hash matches the `hash` stored in the file's front matter, the file is skipped to avoid unnecessary updates.
3.  **Mermaid Diagrams**: It finds all ` ```mermaid ... ``` ` code blocks, renders them into PNG images using `mmdc`, uploads the images to Shortcut, and replaces the code blocks with Markdown image links.
4.  **Local Images**: It finds all local image references (e.g., `![alt](./path/to/image.png)`), uploads them to Shortcut, and replaces the local paths with the new remote URLs.
5.  If the document has an `id` in its front matter, it's updated (PUT). Otherwise, a new document is created (POST).
6.  After a successful creation or update, the file's front matter is updated with the latest metadata from Shortcut, including a new `hash`.

### Clean Unreferenced Files

Delete files from Shortcut that are no longer referenced in any document. This is useful for cleaning up old images or diagrams.

```bash
python src/shortcut_tools/main.py clean-unreferenced-files
```

**Options**:
- `--dry-run`: Print a list of files that would be deleted without actually deleting them.

**Cleaning Process**:
1.  The script fetches all documents and scans their content for links to `media.app.shortcut.com`.
2.  It gets a list of all files uploaded to Shortcut.
3.  It identifies files that are not referenced in any document and are not attached to any stories.
4.  It deletes the unreferenced files.
