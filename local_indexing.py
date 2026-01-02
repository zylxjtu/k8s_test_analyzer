"""
Local indexing module for k8s-test-analyzer.
Handles ChromaDB initialization, document loading, and indexing.
"""

import os
import logging
from typing import List, Set

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

# Global variables
config = None
chroma_client = None
embedding_function = None


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


def process_and_index_documents(
    documents: List[Document],
    collection_name: str,
    persist_directory: str
) -> None:
    """Process documents with CodeSplitter and index them in ChromaDB."""
    if not documents:
        logger.warning("No documents to process.")
        return

    try:
        # Try to get collection if it exists or create a new one
        collection = chroma_client.get_or_create_collection(
            name=collection_name,
            embedding_function=embedding_function,
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
                    # Fall back to text-based splitting
                    nodes = _split_text_to_nodes(doc.text, file_path, file_name)
            else:
                # For non-code files, manually split by lines
                nodes = _split_text_to_nodes(doc.text, file_path, file_name)

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

                chunk_id = f"{file_path}_{start_line}_{end_line}_{i}"

                metadata = {
                    "file_path": file_path,
                    "file_name": file_name,
                    "language": language,
                    "start_line": start_line,
                    "end_line": end_line,
                }

                ids.append(chunk_id)
                texts.append(node.text)
                metadatas.append(metadata)

            # Add nodes to ChromaDB collection
            collection.upsert(
                ids=ids,
                documents=texts,
                metadatas=metadatas
            )

            total_nodes += len(nodes)

        except Exception as e:
            logger.error(
                f"Error processing document "
                f"{doc.metadata.get('file_path', 'unknown')}: {e}"
            )

    logger.info(
        f"Successfully indexed {total_nodes} code chunks "
        f"across {len(documents)} files"
    )


def _split_text_to_nodes(text: str, file_path: str, file_name: str) -> List[TextNode]:
    """Split text into nodes using line-based chunking."""
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

        node = TextNode(
            text=chunk_text,
            metadata={
                "start_line_number": start_idx + 1,
                "end_line_number": end_idx,
                "file_path": file_path,
                "file_name": file_name,
            }
        )
        nodes.append(node)

    return nodes


async def index_project(project_name: str, force: bool = False) -> dict:
    """Index a project folder after download.
    
    Args:
        project_name: The GCS job name / project folder name
        force: If True, re-index even if collection exists
        
    Returns:
        dict with indexing results
    """
    try:
        folder_path = os.path.join(config["projects_root"], project_name)
        
        if not os.path.exists(folder_path):
            return {"success": False, "error": f"Folder not found: {project_name} (looked in {folder_path})"}
        
        collection_name = sanitize_collection_name(project_name)
        
        # Check if collection already exists
        if not force:
            try:
                existing = chroma_client.get_collection(
                    name=collection_name,
                    embedding_function=embedding_function
                )
                doc_count = existing.count()
                logger.info(f"Collection {collection_name} already exists with {doc_count} chunks, skipping")
                return {
                    "success": True,
                    "project": project_name,
                    "collection": collection_name,
                    "message": "Already indexed",
                    "documents_indexed": 0,
                    "existing_chunks": doc_count
                }
            except Exception:
                pass  # Collection doesn't exist, proceed with indexing
        
        # Load all documents from the folder
        documents = load_documents(
            folder_path,
            ignore_dirs=set(config["ignore_dirs"]),
            file_extensions=set(config["file_extensions"]),
            ignore_files=set(config["ignore_files"])
        )
        
        if not documents:
            return {
                "success": True,
                "project": project_name,
                "message": "No indexable documents found",
                "documents_indexed": 0
            }
        
        # Delete existing collection to re-index fresh (only reached if force=True or collection didn't exist)
        try:
            chroma_client.delete_collection(name=collection_name)
            logger.info(f"Deleted existing collection: {collection_name}")
        except Exception:
            pass  # Collection doesn't exist, that's fine
        
        # Index documents
        process_and_index_documents(documents, collection_name, "chroma_db")
        
        logger.info(f"Successfully indexed {len(documents)} documents for {project_name}")
        return {
            "success": True,
            "project": project_name,
            "collection": collection_name,
            "documents_indexed": len(documents)
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
