"""
Local indexing module for k8s-test-analyzer.
Handles ChromaDB initialization, document loading, and indexing.
"""

import gc
import os
import logging
import signal
import time
from typing import Iterator, List, Set
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions
from tree_sitter_language_pack import get_parser

from llama_index.core import Document
from llama_index.core.node_parser import CodeSplitter
from llama_index.core import SimpleDirectoryReader
from llama_index.core.schema import TextNode

logger = logging.getLogger(__name__)

# Default directories to ignore
DEFAULT_IGNORE_DIRS = {
    "__pycache__",
    "node_modules",
    ".git",
    "build",
    "dist",
    ".venv",
    "venv",
    "env",
    ".pytest_cache",
    ".ipynb_checkpoints"
}

# Default files to ignore
DEFAULT_IGNORE_FILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Gemfile.lock",
    "composer.lock",
    ".DS_Store",
    ".env",
    ".env.local",
    ".env.development",
    ".env.test",
    ".env.production",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.so",
    "*.dll",
    "*.dylib",
    ".coverage",
    "coverage.xml",
    ".eslintcache",
    ".tsbuildinfo"
}

# Default file extensions to include
DEFAULT_FILE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rb", ".php", ".swift", ".kt", ".rs", ".scala", ".sh",
    ".html", ".css", ".sql", ".md", ".json", ".yaml", ".yml", ".toml",
    ".txt", ".log", ".xml"  # Include log files and test outputs
}

# Maximum file size to index (in bytes). Files larger than this are skipped to prevent OOM.
# Node logs like containerd.log and kubelet.log can be 100-500MB each; indexing them causes
# 10+ GB memory spikes when multiple builds are indexed simultaneously.
# Configurable via MAX_INDEX_FILE_SIZE_MB environment variable (default: 50MB).
_env_max_mb = int(os.environ.get("MAX_INDEX_FILE_SIZE_MB", "50"))
MAX_INDEX_FILE_SIZE_BYTES = _env_max_mb * 1024 * 1024

# Global variables
config = None
chroma_client = None
embedding_function = None

# Asyncio lock for serializing indexing operations
# This prevents concurrent writes to ChromaDB which can corrupt the HNSW index
# Note: Lock is created lazily via _get_indexing_lock() to ensure it's in the correct event loop
import asyncio
_indexing_lock = None

def _get_indexing_lock():
    """Get or create the indexing lock in the current event loop.

    Creating asyncio.Lock at module import time can cause issues if the lock
    is bound to a different event loop than the one running the async code.
    """
    global _indexing_lock
    if _indexing_lock is None:
        _indexing_lock = asyncio.Lock()
    return _indexing_lock

# Threading lock for completion marker updates (sync operations)
import threading
_completion_marker_lock = threading.Lock()

# Heartbeat for health monitoring
_HEARTBEAT_PATH = None

def _get_heartbeat_path():
    """Get heartbeat file path based on projects_root."""
    global _HEARTBEAT_PATH
    if _HEARTBEAT_PATH is None:
        projects_root = os.getenv("PROJECTS_ROOT", os.path.expanduser("~/.k8s_testlog_cache"))
        _HEARTBEAT_PATH = os.path.join(projects_root, ".indexing_heartbeat")
    return _HEARTBEAT_PATH

def write_heartbeat():
    """Write current timestamp to heartbeat file (atomic).

    Used by Docker healthcheck to detect hung indexing processes.
    """
    try:
        path = _get_heartbeat_path()
        temp = path + ".tmp"
        with open(temp, "w") as f:
            f.write(str(time.time()))
        os.replace(temp, path)  # Atomic rename
    except Exception as e:
        logger.warning(f"Failed to write heartbeat: {e}")


def sanitize_collection_name(folder_name: str) -> str:
    """Convert folder name to a valid collection name by replacing forward slashes with underscores."""
    return folder_name.replace("/", "_")


def auto_discover_folders(projects_root: str, ignore_dirs: Set[str]) -> List[str]:
    """
    Auto-discover all first-level subdirectories in projects_root.

    Args:
        projects_root: Root directory to scan
        ignore_dirs: Set of directory names to ignore

    Returns:
        List of relative folder paths to index
    """
    discovered_folders = []

    try:
        if not os.path.exists(projects_root):
            logger.warning(f"Projects root does not exist: {projects_root}")
            return []

        # List all items in projects_root
        for item in os.listdir(projects_root):
            item_path = os.path.join(projects_root, item)

            # Skip if not a directory
            if not os.path.isdir(item_path):
                continue

            # Skip if it's a symlink
            if os.path.islink(item_path):
                logger.info(f"Skipping symlink: {item}")
                continue

            # Skip if in ignore list or starts with dot
            if item in ignore_dirs or item.startswith('.'):
                logger.info(f"Skipping ignored directory: {item}")
                continue

            discovered_folders.append(item)

        logger.info(f"Auto-discovered {len(discovered_folders)} folders: {discovered_folders}")
        return discovered_folders

    except Exception as e:
        logger.error(f"Error during auto-discovery: {e}")
        return []


def get_config_from_env():
    """
    Get configuration from environment variables.

    Supports two modes:
    1. Manual mode: Set FOLDERS_TO_INDEX with specific folders (comma-separated)
    2. Auto mode (default): When FOLDERS_TO_INDEX is empty, automatically discovers
       all first-level subdirectories in PROJECTS_ROOT
    
    Note: FASTMCP_PORT is only required when running the MCP server, not for CLI usage.
    """
    # Use same default as k8s_testlog_downloader for cache directory
    projects_root = os.path.expanduser(os.getenv("PROJECTS_ROOT", "~/.k8s-test-analyzer/cache"))

    # Get manual folder configuration
    folders_to_index = os.getenv("FOLDERS_TO_INDEX", "").split(",")
    folders_to_index = [f.strip() for f in folders_to_index if f.strip()]

    # Get additional ignore dirs and files from environment
    additional_ignore_dirs = os.getenv(
        "ADDITIONAL_IGNORE_DIRS", ""
    ).split(",")
    additional_ignore_dirs = [
        d.strip() for d in additional_ignore_dirs if d.strip()
    ]

    additional_ignore_files = os.getenv(
        "ADDITIONAL_IGNORE_FILES", ""
    ).split(",")
    additional_ignore_files = [
        f.strip() for f in additional_ignore_files if f.strip()
    ]

    # Combine default and additional ignore patterns
    ignore_dirs = list(DEFAULT_IGNORE_DIRS | set(additional_ignore_dirs))
    ignore_files = list(DEFAULT_IGNORE_FILES | set(additional_ignore_files))

    # Determine which folders to index
    if folders_to_index:
        # Manual configuration provided
        logger.info(f"Using manually configured folders: {folders_to_index}")
    else:
        # Auto-discover folders (default behavior)
        logger.info("FOLDERS_TO_INDEX is empty, auto-discovering folders...")
        folders_to_index = auto_discover_folders(projects_root, set(ignore_dirs))
        if not folders_to_index:
            logger.warning("No folders discovered. Using root directory.")
            folders_to_index = [""]

    return {
        "projects_root": projects_root,
        "folders_to_index": folders_to_index,
        "ignore_dirs": ignore_dirs,
        "ignore_files": ignore_files,
        "file_extensions": list(DEFAULT_FILE_EXTENSIONS)
    }


async def initialize_chromadb():
    """Initialize ChromaDB and embedding function asynchronously."""
    global config, chroma_client, embedding_function

    try:
        # Get configuration from environment
        config = get_config_from_env()
        logger.info("Configuration loaded successfully")

        # Initialize ChromaDB client with telemetry disabled
        # Use chroma_db under PROJECTS_ROOT so Docker and CLI share the same database
        chroma_db_path = os.path.join(config["projects_root"], "chroma_db")
        os.makedirs(chroma_db_path, exist_ok=True)
        
        chroma_client = chromadb.PersistentClient(
            path=chroma_db_path,
            settings=Settings(anonymized_telemetry=False)
        )
        logger.info(f"ChromaDB client initialized at {chroma_db_path}")

        # Initialize embedding function
        embedding_function = (
            embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"  # Faster model for quicker indexing
            )
        )
        logger.info("Embedding function initialized")

        return True
    except Exception as e:
        logger.error(f"Error during initialization: {e}")
        # Still need to assign default values to global variables
        if config is None:
            config = {"projects_root": "", "folders_to_index": [""]}
        if chroma_client is None:
            # Create an empty client as a fallback
            try:
                fallback_path = os.path.join(os.path.expanduser("~/.k8s-test-analyzer/cache"), "chroma_db")
                os.makedirs(fallback_path, exist_ok=True)
                chroma_client = chromadb.PersistentClient(path=fallback_path)
            except Exception as db_err:
                logger.error(
                    f"Failed to create fallback ChromaDB client: {db_err}"
                )
        if embedding_function is None:
            try:
                embedding_function = (
                    embedding_functions.SentenceTransformerEmbeddingFunction(
                        model_name="all-MiniLM-L6-v2"
                    )
                )
            except Exception as embed_err:
                logger.error(
                    f"Failed to create fallback embedding function: {embed_err}"
                )
        return False


def is_valid_file(
    file_path: str,
    ignore_dirs: Set[str],
    file_extensions: Set[str],
    ignore_files: Set[str] = None
) -> bool:
    """Check if a file should be processed based on its path and extension."""
    # Check if path contains ignored directory
    parts = file_path.split(os.path.sep)
    for part in parts:
        if part in ignore_dirs:
            return False

    # Get file name and check against ignored files
    file_name = os.path.basename(file_path)

    # Use provided ignore_files or fall back to default
    files_to_ignore = ignore_files if ignore_files is not None else DEFAULT_IGNORE_FILES

    # Check exact matches
    if file_name in files_to_ignore:
        return False

    # Check wildcard patterns
    for pattern in files_to_ignore:
        if pattern.startswith("*"):
            if file_name.endswith(pattern[1:]):
                return False

    # Check file extension
    _, ext = os.path.splitext(file_path)
    return ext.lower() in file_extensions if file_extensions else True


def load_documents(
    directory: str, 
    ignore_dirs: Set[str] = DEFAULT_IGNORE_DIRS,
    file_extensions: Set[str] = DEFAULT_FILE_EXTENSIONS,
    ignore_files: Set[str] = None
) -> List[Document]:
    """
    Load documents from a directory, filtering out ignored paths.
    Uses os.walk with followlinks=False to avoid following symbolic links.
    """
    try:
        # Get all files recursively - DO NOT follow symlinks
        all_files = []
        for root, dirs, files in os.walk(directory, followlinks=False):
            # Skip ignored directories
            dirs[:] = [
                d for d in dirs
                if d not in ignore_dirs and not d.startswith('.')
            ]

            for file in files:
                abs_file_path = os.path.join(root, file)

                # Skip symlinks completely to avoid issues
                if os.path.islink(abs_file_path):
                    continue

                if is_valid_file(
                    abs_file_path,
                    ignore_dirs,
                    file_extensions,
                    ignore_files
                ):
                    # Skip files that exceed the size limit to prevent OOM during indexing.
                    # Large node logs (containerd.log, kubelet.log) can be 100-500MB each.
                    try:
                        file_size = os.path.getsize(abs_file_path)
                        if file_size > MAX_INDEX_FILE_SIZE_BYTES:
                            rel_file_path = os.path.relpath(abs_file_path, directory)
                            logger.info(
                                f"Skipping large file (>{MAX_INDEX_FILE_SIZE_BYTES // (1024*1024)}MB): "
                                f"{rel_file_path} ({file_size / (1024*1024):.1f}MB)"
                            )
                            continue
                    except OSError:
                        continue

                    # Calculate relative path from the directory being indexed
                    rel_file_path = os.path.relpath(abs_file_path, directory)
                    all_files.append((abs_file_path, rel_file_path))

        if not all_files:
            logger.warning(f"No valid files found in {directory}")
            return []

        # Load the filtered files using absolute paths for reading
        reader = SimpleDirectoryReader(
            input_files=[abs_path for abs_path, _ in all_files],
            exclude_hidden=True
        )
        documents = reader.load_data()

        # Update the metadata to use relative paths
        for doc, (_, rel_path) in zip(documents, all_files):
            doc.metadata["file_path"] = rel_path

        logger.info(f"Loaded {len(documents)} documents from {directory}")
        return documents
    except Exception as e:
        logger.error(f"Error loading documents: {e}")
        return []


def iter_documents(
    directory: str,
    ignore_dirs: Set[str] = DEFAULT_IGNORE_DIRS,
    file_extensions: Set[str] = DEFAULT_FILE_EXTENSIONS,
    ignore_files: Set[str] = None,
) -> Iterator[Document]:
    """Yield Documents one file at a time so the caller never holds the
    whole project in memory simultaneously. Same filtering rules as
    load_documents."""
    for root, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith('.')]
        for file in files:
            abs_file_path = os.path.join(root, file)
            if os.path.islink(abs_file_path):
                continue
            if not is_valid_file(abs_file_path, ignore_dirs, file_extensions, ignore_files):
                continue
            try:
                file_size = os.path.getsize(abs_file_path)
            except OSError:
                continue
            rel_file_path = os.path.relpath(abs_file_path, directory)
            if file_size > MAX_INDEX_FILE_SIZE_BYTES:
                logger.info(
                    f"Skipping large file (>{MAX_INDEX_FILE_SIZE_BYTES // (1024*1024)}MB): "
                    f"{rel_file_path} ({file_size / (1024*1024):.1f}MB)"
                )
                continue
            try:
                reader = SimpleDirectoryReader(input_files=[abs_file_path], exclude_hidden=True)
                docs = reader.load_data()
            except Exception as e:
                logger.error(f"Error loading {abs_file_path}: {e}")
                continue
            for doc in docs:
                doc.metadata["file_path"] = rel_file_path
                yield doc


def index_build_streaming(
    build_path: str,
    build_id: str,
    collection_name: str,
    ignore_dirs: Set[str],
    file_extensions: Set[str],
    ignore_files: Set[str],
) -> int:
    """Index one build's documents streaming-style: load → tag → upsert one
    file at a time so peak working set is one file, not the whole build.
    Runs sync (call from asyncio.to_thread). Returns file count processed."""
    file_count = 0
    for doc in iter_documents(build_path, ignore_dirs, file_extensions, ignore_files):
        if hasattr(doc, "metadata"):
            doc.metadata["build_id"] = build_id
        process_and_index_documents([doc], collection_name, "chroma_db")
        file_count += 1
        del doc
    # Reclaim transient embedding/chunk arrays before next build's load
    gc.collect()
    return file_count


def process_and_index_documents(
    documents: List[Document],
    collection_name: str,
    persist_directory: str
) -> None:
    """Process documents with CodeSplitter and index them in ChromaDB."""
    import time
    start_time = time.time()

    if not documents:
        logger.warning("No documents to process.")
        return

    if chroma_client is None:
        logger.error("ChromaDB not initialized — skipping indexing of %d documents", len(documents))
        return

    try:
        # Try to get collection if it exists or create a new one
        collection = chroma_client.get_or_create_collection(
            name=collection_name,
            # Note: embedding_function intentionally NOT passed to avoid ChromaDB 1.x
            # ONNX "precompile already registered" error. Embeddings are pre-computed instead.
            metadata={"hnsw:space": "cosine"}
        )
    except Exception as e:
        logger.error(f"Error creating collection: {e}")
        return

    # Process each document
    total_nodes = 0
    for doc in documents:
        try:
            # Extract file path from metadata
            file_path = doc.metadata.get("file_path", "unknown")
            file_name = os.path.basename(file_path)

            # Determine language from file extension
            _, ext = os.path.splitext(file_name)
            language = ext[1:] if ext else "text"  # Remove the dot

            # Handle Markdown and other text files differently
            code_file_extensions = [
                "py", "python", "js", "jsx", "ts", "tsx", "java", "c", 
                "cpp", "h", "hpp", "cs", "go", "rb", "php", "swift", 
                "kt", "rs", "scala"
            ]

            if language in code_file_extensions:
                # Determine parser language based on file extension
                parser_language = "python"  # Default fallback
                if language in ["py", "python"]:
                    parser_language = "python"
                elif language in ["js", "jsx", "ts", "tsx"]:
                    parser_language = "javascript"
                elif language in ["java"]:
                    parser_language = "java"
                elif language in ["c", "cpp", "h", "hpp"]:
                    parser_language = "cpp"
                elif language in ["cs"]:
                    parser_language = "csharp"
                elif language in ["go"]:
                    parser_language = "go"
                elif language in ["rb"]:
                    parser_language = "ruby"
                elif language in ["php"]:
                    parser_language = "php"
                elif language in ["swift"]:
                    parser_language = "swift"
                elif language in ["kt"]:
                    parser_language = "kotlin"
                elif language in ["rs"]:
                    parser_language = "rust"
                elif language in ["scala"]:
                    parser_language = "scala"

                # Create parser and splitter for this specific language
                try:
                    code_parser = get_parser(parser_language)
                    splitter = CodeSplitter(
                        language=parser_language,
                        chunk_lines=40,
                        chunk_lines_overlap=15,
                        max_chars=1500,
                        parser=code_parser
                    )
                    nodes = splitter.get_nodes_from_documents([doc])
                except Exception as e:
                    logger.warning(
                        f"Could not create parser for {parser_language}, "
                        f"falling back to text-based splitting: {e}"
                    )
                    # Fall back to text-based splitting, pass build_id if available
                    extra_meta = {"build_id": doc.metadata.get("build_id")} if doc.metadata.get("build_id") else None
                    nodes = _split_text_to_nodes(doc.text, file_path, file_name, extra_meta)
            else:
                # For non-code files, manually split by lines, pass build_id if available
                extra_meta = {"build_id": doc.metadata.get("build_id")} if doc.metadata.get("build_id") else None
                nodes = _split_text_to_nodes(doc.text, file_path, file_name, extra_meta)

            if not nodes:
                logger.warning(f"No nodes generated for {file_path}")
                continue

            logger.info(f"Processing {file_path}: {len(nodes)} chunks")

            # Prepare data for ChromaDB
            ids = []
            texts = []
            metadatas = []

            for i, node in enumerate(nodes):
                start_line = node.metadata.get("start_line_number", 0)
                end_line = node.metadata.get("end_line_number", 0)

                if start_line == 0 or end_line == 0:
                    start_line = 1
                    end_line = len(node.text.split("\n"))

                # Include build_id in chunk_id to prevent collisions between builds
                # Different jobs/tabs have separate collections, but within a collection
                # multiple builds may have same file paths - build_id ensures uniqueness
                build_id = node.metadata.get("build_id", "")
                if build_id:
                    chunk_id = f"{build_id}_{file_path}_{start_line}_{end_line}_{i}"
                else:
                    chunk_id = f"{file_path}_{start_line}_{end_line}_{i}"

                metadata = {
                    "file_path": file_path,
                    "file_name": file_name,
                    "language": language,
                    "start_line": start_line,
                    "end_line": end_line,
                }

                # Include build_id from node metadata (inherited from document)
                if node.metadata.get("build_id"):
                    metadata["build_id"] = node.metadata["build_id"]

                ids.append(chunk_id)
                texts.append(node.text)
                metadatas.append(metadata)

            # Pre-compute embeddings to avoid ChromaDB 1.x internally
            # reconstructing the ONNX embedding function from stored config,
            # which triggers "Artifact of type=precompile already registered" error.
            embeddings = embedding_function(texts) if embedding_function is not None else None

            # ChromaDB max batch size is 5461 - split large files into batches
            CHROMA_MAX_BATCH = 5000
            for batch_start in range(0, len(ids), CHROMA_MAX_BATCH):
                batch_end = batch_start + CHROMA_MAX_BATCH
                upsert_kwargs = dict(
                    ids=ids[batch_start:batch_end],
                    documents=texts[batch_start:batch_end],
                    metadatas=metadatas[batch_start:batch_end]
                )
                if embeddings is not None:
                    upsert_kwargs["embeddings"] = embeddings[batch_start:batch_end]
                collection.upsert(**upsert_kwargs)

            total_nodes += len(nodes)

            # Update heartbeat after each file to indicate progress
            write_heartbeat()

        except Exception as e:
            logger.error(
                f"Error processing document "
                f"{doc.metadata.get('file_path', 'unknown')}: {e}"
            )

    elapsed_time = time.time() - start_time
    logger.info(
        f"Successfully indexed {total_nodes} code chunks "
        f"across {len(documents)} files in {elapsed_time:.2f}s"
    )


def _split_text_to_nodes(text: str, file_path: str, file_name: str, extra_metadata: dict = None) -> List[TextNode]:
    """Split text into nodes using line-based chunking.

    Args:
        text: The text content to split
        file_path: Path to the file
        file_name: Name of the file
        extra_metadata: Additional metadata to include in each node (e.g., build_id)
    """
    nodes = []
    lines = text.split("\n")
    chunk_size = 40
    overlap = 15

    for i in range(0, len(lines), chunk_size - overlap):
        start_idx = i
        end_idx = min(i + chunk_size, len(lines))

        if start_idx >= len(lines):
            continue

        chunk_text = "\n".join(lines[start_idx:end_idx])

        if not chunk_text.strip():
            continue

        metadata = {
            "start_line_number": start_idx + 1,
            "end_line_number": end_idx,
            "file_path": file_path,
            "file_name": file_name,
        }

        # Include any extra metadata (like build_id) from the parent document
        if extra_metadata:
            metadata.update(extra_metadata)

        node = TextNode(
            text=chunk_text,
            metadata=metadata
        )
        nodes.append(node)

    return nodes


def _marker_file_path(collection_name: str) -> str:
    """Filesystem path for the completion marker file for a collection.

    Stored under projects_root so it lives next to the build data, survives
    container restarts (bind-mounted), and is independent of the ChromaDB
    storage layer.
    """
    safe = collection_name.replace("/", "_")
    return os.path.join(config["projects_root"], f".completed_builds_{safe}.txt")


def _write_completed_builds(collection_name: str, completed: set) -> bool:
    """Atomically write the completion marker file."""
    try:
        marker_path = _marker_file_path(collection_name)
        tmp_path = marker_path + ".tmp"
        with open(tmp_path, "w") as f:
            for build_id in sorted(completed):
                f.write(f"{build_id}\n")
        os.replace(tmp_path, marker_path)
        return True
    except Exception as e:
        logger.error(f"Error writing completion marker for {collection_name}: {e}")
        return False


def _get_completed_builds(collection_name: str) -> set:
    """Get set of build IDs that have been fully indexed in a collection.

    Reads from a filesystem marker file so we never touch ChromaDB on the
    "already indexed?" hot path (each get_collection / get() call forces a
    HNSW segment load into RAM ~500 MB per collection).

    On first call after upgrade, if the file is missing, falls back to the
    legacy in-collection marker document and migrates it to the file.

    Args:
        collection_name: Name of the ChromaDB collection

    Returns:
        Set of build ID strings that are fully indexed
    """
    try:
        marker_path = _marker_file_path(collection_name)
        if os.path.exists(marker_path):
            with open(marker_path) as f:
                return {line.strip() for line in f if line.strip()}

        # One-time migration from legacy chromadb-stored marker.
        if chroma_client is None:
            return set()
        try:
            collection = chroma_client.get_collection(name=collection_name)
            result = collection.get(ids=["_completed_builds_marker"], include=["metadatas"])
            if result and result.get("metadatas") and result["metadatas"]:
                completed_str = result["metadatas"][0].get("completed_builds", "")
                if completed_str:
                    migrated = set(completed_str.split(","))
                    _write_completed_builds(collection_name, migrated)
                    logger.info(f"Migrated {len(migrated)} completion markers to filesystem for {collection_name}")
                    return migrated
        except Exception:
            pass
        return set()
    except Exception as e:
        logger.error(f"Error reading completed builds for {collection_name}: {e}")
        return set()


def _mark_build_completed(collection_name: str, build_id: str) -> bool:
    """Mark a build as fully indexed by adding it to the completion marker.

    Uses a lock to prevent race conditions when multiple tasks mark builds
    as completed simultaneously (read-modify-write pattern).

    Args:
        collection_name: Name of the ChromaDB collection
        build_id: Build ID that finished indexing

    Returns:
        True if successful, False otherwise
    """
    with _completion_marker_lock:
        completed = _get_completed_builds(collection_name)
        completed.add(str(build_id))
        ok = _write_completed_builds(collection_name, completed)
        if ok:
            logger.debug(f"Marked build {build_id} as completed in {collection_name}")
        return ok


def _unmark_build_completed(collection_name: str, build_id: str) -> bool:
    """Remove a build from the completion marker (e.g., when deleting a build).

    Uses a lock to prevent race conditions when multiple tasks modify the
    completion marker simultaneously (read-modify-write pattern).

    Args:
        collection_name: Name of the ChromaDB collection
        build_id: Build ID to remove from completed list

    Returns:
        True if successful, False otherwise
    """
    with _completion_marker_lock:
        completed = _get_completed_builds(collection_name)
        completed.discard(str(build_id))
        ok = _write_completed_builds(collection_name, completed)
        if ok:
            logger.debug(f"Removed build {build_id} from completed list in {collection_name}")
        return ok
def _get_indexed_build_ids(collection_name: str) -> set:
    """Get set of build IDs that are already indexed in a collection.

    Note: This returns all builds with chunks in the collection, including
    partially indexed builds. Use _get_completed_builds() to check for
    builds that finished indexing completely.

    Args:
        collection_name: Name of the ChromaDB collection

    Returns:
        Set of build ID strings that have any chunks indexed
    """
    try:
        if chroma_client is None:
            return set()

        collection = chroma_client.get_collection(name=collection_name)

        # Get all metadatas to extract build IDs
        all_docs = collection.get(include=["metadatas"])

        if not all_docs or not all_docs.get("metadatas"):
            return set()

        indexed_builds = set()
        for metadata in all_docs["metadatas"]:
            # Skip marker documents
            if metadata.get("is_marker"):
                continue
            # Check for explicit build_id metadata field
            build_id = metadata.get("build_id")
            if build_id:
                indexed_builds.add(str(build_id))

        return indexed_builds
    except Exception:
        return set()


def _get_build_folders(folder_path: str) -> list:
    """Get list of build folders (numeric IDs) in a project folder.

    Args:
        folder_path: Path to the project folder

    Returns:
        List of build ID strings found in the folder
    """
    build_folders = []
    try:
        for item in os.listdir(folder_path):
            item_path = os.path.join(folder_path, item)
            if os.path.isdir(item_path) and item.isdigit():
                build_folders.append(item)
    except Exception:
        pass
    return build_folders


async def index_project(project_name: str, force: bool = False) -> dict:
    """Index a project folder after download.

    This function supports incremental indexing - it will only index new builds
    that aren't already in the collection. Use force=True to re-index everything.

    If indexing is interrupted, partially indexed builds will be re-indexed on
    the next run (only fully completed builds are marked as done).

    IMPORTANT: This function acquires an async lock to prevent concurrent indexing
    operations which can corrupt ChromaDB's HNSW index.

    Args:
        project_name: The GCS job name / project folder name
        force: If True, delete existing collection and re-index everything

    Returns:
        dict with indexing results
    """
    # Acquire lock to prevent concurrent indexing (protects ChromaDB HNSW index)
    async with _get_indexing_lock():
        return await _index_project_impl(project_name, force)


async def _index_project_impl(project_name: str, force: bool = False) -> dict:
    """Internal implementation of index_project (called with lock held)."""
    try:
        folder_path = os.path.join(config["projects_root"], project_name)

        if not os.path.exists(folder_path):
            return {"success": False, "error": f"Folder not found: {project_name} (looked in {folder_path})"}

        collection_name = sanitize_collection_name(project_name)

        # Get build folders in the project directory
        all_build_folders = _get_build_folders(folder_path)

        if force:
            # Force mode: delete existing collection and re-index everything
            try:
                chroma_client.delete_collection(name=collection_name)
                logger.info(f"Deleted existing collection: {collection_name}")
            except Exception:
                pass
            builds_to_index = set(all_build_folders)
            completed_builds = set()
        else:
            # Incremental mode: only index builds not marked as completed
            # This uses completion markers, not just presence of chunks
            completed_builds = _get_completed_builds(collection_name)
            builds_to_index = set(all_build_folders) - completed_builds

            if not builds_to_index:
                # Don't touch the chromadb collection here. get_collection() +
                # count() on chromadb 1.x forces the HNSW graph into memory
                # (~500 MB per collection) — and on a steady-state cycle every
                # project hits this branch, so we'd load every collection just
                # for a status field that nothing reads. Skip it entirely.
                logger.info(f"All {len(all_build_folders)} builds already indexed for {project_name}")
                return {
                    "success": True,
                    "project": project_name,
                    "collection": collection_name,
                    "message": "Already indexed",
                    "documents_indexed": 0,
                    "builds_indexed": list(completed_builds)
                }

        logger.info(f"Indexing {len(builds_to_index)} new builds for {project_name} "
                   f"({len(completed_builds)} already completed)")

        # Index each build separately so we can mark completion individually
        # This handles interruption gracefully - partially indexed builds will be re-done
        total_documents = 0
        newly_indexed = []

        for build_id in builds_to_index:
            build_path = os.path.join(folder_path, build_id)

            # Check if this build has partial chunks (from interrupted indexing)
            # If so, delete them before re-indexing
            partial_chunks = await asyncio.to_thread(_get_indexed_build_ids, collection_name)
            if build_id in partial_chunks and build_id not in completed_builds:
                logger.info(f"Removing partial index for interrupted build {build_id}")
                await asyncio.to_thread(delete_build_from_index, collection_name, build_id)

            # Stream files through the indexer one at a time so peak RAM
            # is one file's chunks/embeddings, not the whole build's.
            file_count = await asyncio.to_thread(
                index_build_streaming,
                build_path,
                build_id,
                collection_name,
                set(config["ignore_dirs"]),
                set(config["file_extensions"]),
                set(config["ignore_files"]),
            )

            if file_count == 0:
                # No documents but mark as completed (empty build)
                _mark_build_completed(collection_name, build_id)
                newly_indexed.append(build_id)
                continue

            # Mark build as completed AFTER successful indexing
            _mark_build_completed(collection_name, build_id)

            total_documents += file_count
            newly_indexed.append(build_id)
            logger.info(f"Completed indexing build {build_id}: {file_count} documents")

        if not newly_indexed:
            return {
                "success": True,
                "project": project_name,
                "message": "No indexable documents found in new builds",
                "documents_indexed": 0,
                "builds_checked": list(builds_to_index)
            }

        total_indexed = len(completed_builds) + len(newly_indexed)
        logger.info(f"Successfully indexed {total_documents} documents from {len(newly_indexed)} new builds for {project_name}")
        return {
            "success": True,
            "project": project_name,
            "collection": collection_name,
            "documents_indexed": total_documents,
            "new_builds_indexed": newly_indexed,
            "total_builds_indexed": total_indexed
        }

    except Exception as e:
        logger.error(f"Error indexing project {project_name}: {e}")
        return {"success": False, "error": str(e)}


async def perform_initial_indexing():
    """Index all existing folders in projects_root at startup."""
    try:
        projects_root = config["projects_root"]
        if not os.path.exists(projects_root):
            logger.warning(f"Projects root does not exist: {projects_root}")
            return
        
        # Get all folders to index (auto-discovered or configured)
        folders = config.get("folders_to_index", [])
        if not folders:
            folders = auto_discover_folders(projects_root, set(config["ignore_dirs"]))
        
        if not folders:
            logger.info("No folders found to index at startup")
            return
        
        logger.info(f"Starting initial indexing of {len(folders)} folders...")
        
        for folder in folders:
            if not folder:  # Skip empty folder names
                continue
            result = await index_project(folder)
            if result.get("success"):
                logger.info(f"Indexed {folder}: {result.get('documents_indexed', 0)} documents")
            else:
                logger.warning(f"Failed to index {folder}: {result.get('error', 'Unknown error')}")
        
        logger.info("Initial indexing complete")
        
    except Exception as e:
        logger.error(f"Error during initial indexing: {e}")


def get_chroma_client():
    """Get the ChromaDB client."""
    return chroma_client


def get_embedding_function():
    """Get the embedding function."""
    return embedding_function


def get_config():
    """Get the current configuration."""
    return config


def get_indexing_lock():
    """Get the indexing lock for use by external code.

    This lock should be acquired before any ChromaDB write operations
    to prevent concurrent writes that can corrupt the HNSW index.

    Lazy-inits on first call (same as the internal _get_indexing_lock) so the
    standalone cleanup_worker subprocess gets a real lock instead of None.

    Usage:
        async with get_indexing_lock():
            # Perform ChromaDB write operations

    Returns:
        asyncio.Lock instance
    """
    return _get_indexing_lock()


def delete_build_from_index(collection_name: str, build_id: str) -> dict:
    """
    Delete all indexed chunks for a specific build from a ChromaDB collection.

    Uses ChromaDB's `where` clause for efficient server-side filtering by build_id.

    Args:
        collection_name: Name of the collection (job name)
        build_id: Build ID whose chunks should be deleted

    Returns:
        dict with deletion results
    """
    try:
        if chroma_client is None:
            return {"success": False, "error": "ChromaDB client not initialized"}

        # Get the collection
        try:
            collection = chroma_client.get_collection(name=collection_name)
        except Exception as e:
            return {"success": False, "error": f"Collection not found: {collection_name}"}

        # Use where filter to get only IDs for this build (more efficient)
        result = collection.get(
            where={"build_id": str(build_id)},
            include=[]  # Only need IDs, not content
        )

        if not result or not result.get("ids"):
            return {"success": True, "deleted_chunks": 0, "message": f"No chunks found for build {build_id}"}

        ids_to_delete = result["ids"]

        # Delete the chunks in batches (ChromaDB has limits on batch size)
        batch_size = 5000
        for i in range(0, len(ids_to_delete), batch_size):
            batch = ids_to_delete[i:i + batch_size]
            collection.delete(ids=batch)

        # Also remove from completion marker so it can be re-indexed
        _unmark_build_completed(collection_name, build_id)

        logger.info(f"Deleted {len(ids_to_delete)} chunks for build {build_id} from {collection_name}")
        return {"success": True, "deleted_chunks": len(ids_to_delete)}

    except Exception as e:
        logger.error(f"Error deleting build {build_id} from index: {e}")
        return {"success": False, "error": str(e)}


def get_build_chunks(collection_name: str, build_id: str, include_embeddings: bool = False) -> dict:
    """
    Get all indexed chunks for a specific build from a ChromaDB collection.

    Uses ChromaDB's `where` filter for efficient server-side filtering by build_id,
    rather than fetching all documents and filtering client-side.

    Args:
        collection_name: Name of the collection (job name)
        build_id: Build ID whose chunks should be retrieved
        include_embeddings: Whether to include embeddings in the result

    Returns:
        dict with chunks data including ids, documents, metadatas, and optionally embeddings
    """
    try:
        if chroma_client is None:
            return {"success": False, "error": "ChromaDB client not initialized", "chunks": []}

        # Get the collection
        try:
            collection = chroma_client.get_collection(name=collection_name)
        except Exception:
            return {"success": False, "error": f"Collection not found: {collection_name}", "chunks": []}

        # Use ChromaDB's where filter for efficient server-side filtering
        include_fields = ["metadatas", "documents"]
        if include_embeddings:
            include_fields.append("embeddings")

        # Query with where clause - ChromaDB handles filtering efficiently
        result = collection.get(
            where={"build_id": str(build_id)},
            include=include_fields
        )

        if not result or not result.get("ids"):
            return {"success": True, "chunks": [], "message": f"No chunks found for build {build_id}"}

        # Build chunk data from filtered results
        chunks = []
        for i, (doc_id, metadata, document) in enumerate(zip(
            result["ids"],
            result["metadatas"],
            result["documents"]
        )):
            chunk_data = {
                "id": doc_id,
                "text": document,
                "metadata": metadata,
                "file_path": metadata.get("file_path", "")
            }
            if include_embeddings and result.get("embeddings"):
                chunk_data["embedding"] = result["embeddings"][i]
            chunks.append(chunk_data)

        logger.info(f"Retrieved {len(chunks)} chunks for build {build_id} from {collection_name}")
        return {"success": True, "chunks": chunks, "total": len(chunks)}

    except Exception as e:
        logger.error(f"Error getting chunks for build {build_id}: {e}")
        return {"success": False, "error": str(e), "chunks": []}
