"""
Template Deployer - Deploy Tensorlake applications from GitHub.

This application demonstrates:
- GitHub API integration for dynamic file discovery
- Parallel file fetching using map() - N files = N containers
- SDK internals for manifest generation (same as CLI)
- Multi-stage orchestration with proper error handling

No hardcoded file lists. The deployer discovers what files exist
in a template directory and fetches them all in parallel.
"""

import io
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Optional, List, Dict

import httpx
from tensorlake.applications import application, function, Retries


# Files to exclude from deployment package
EXCLUDED_FILES = {"README.md", "README", ".gitignore", "LICENSE", "__pycache__"}


@dataclass
class DeployRequest:
    """Request to deploy a template from GitHub."""
    template_id: str
    target_namespace: str
    app_name: str
    github_repo: str = "tensorlakeai/examples"
    github_branch: str = "main"


@dataclass
class DeployResult:
    """Result of a deployment operation."""
    success: bool
    app_name: Optional[str] = None
    error: Optional[str] = None
    invoke_url: Optional[str] = None
    files_deployed: List[str] = field(default_factory=list)


@dataclass
class FileToFetch:
    """A file discovered via GitHub API."""
    name: str
    download_url: str


@application(input_deserializer="json", output_serializer="json")
@function(timeout=180, secrets=["TENSORLAKE_DEPLOYER_API_KEY"])
def deploy_template(request: DeployRequest) -> DeployResult:
    """
    Deploy a template from a GitHub repository.

    Pipeline:
    1. Discover files via GitHub API (no hardcoded file list)
    2. Fetch all files in parallel using map()
    3. Generate manifest using SDK internals
    4. Create zip and deploy to target namespace

    This is Tensorlake deploying Tensorlake - the same code path
    as the CLI, orchestrated as a distributed application.
    """
    # Stage 1: Discover files in the template directory
    discovery = discover_template_files(
        repo=request.github_repo,
        branch=request.github_branch,
        template_id=request.template_id,
    )

    if discovery.get("error"):
        return DeployResult(success=False, error=discovery["error"])

    files_to_fetch: List[FileToFetch] = discovery["files"]

    if not files_to_fetch:
        return DeployResult(
            success=False,
            error=f"Template '{request.template_id}' is empty or not found"
        )

    # Validate main.py exists
    if not any(f.name == "main.py" for f in files_to_fetch):
        return DeployResult(
            success=False,
            error=f"Template '{request.template_id}' is missing main.py"
        )

    # Stage 2: Fetch all files in parallel using map()
    # Each file download runs in its own container
    download_urls = [f.download_url for f in files_to_fetch]
    file_contents = fetch_file.map(download_urls)

    # Build files dictionary
    files: Dict[str, str] = {}
    for file_meta, content in zip(files_to_fetch, file_contents):
        if content is None:
            return DeployResult(
                success=False,
                error=f"Failed to download {file_meta.name}"
            )
        files[file_meta.name] = content

    # Stage 3: Generate manifest using SDK internals
    manifest_result = generate_manifest(files["main.py"], request.app_name)

    if manifest_result.get("error"):
        return DeployResult(success=False, error=manifest_result["error"])

    # Stage 4: Create deployment package and deploy
    code_zip = create_zip(files)

    deploy_result = deploy_to_namespace(
        manifest_json=manifest_result["manifest_json"],
        code_zip=code_zip,
        namespace=request.target_namespace,
    )

    if not deploy_result["success"]:
        return DeployResult(success=False, error=deploy_result.get("error"))

    return DeployResult(
        success=True,
        app_name=request.app_name,
        invoke_url=f"https://api.tensorlake.ai/v1/namespaces/{request.target_namespace}/applications/{request.app_name}/invoke",
        files_deployed=list(files.keys()),
    )


@function(timeout=30, retries=Retries(max_retries=2))
def discover_template_files(repo: str, branch: str, template_id: str) -> dict:
    """
    Discover files in a template directory using GitHub Contents API.

    Returns list of files with their download URLs. No hardcoding -
    discovers whatever files exist in the template directory.

    GET /repos/{owner}/{repo}/contents/{path}?ref={branch}
    """
    url = f"https://api.github.com/repos/{repo}/contents/{template_id}"
    params = {"ref": branch}
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "tensorlake-template-deployer",
    }

    try:
        response = httpx.get(url, params=params, headers=headers, timeout=15)

        if response.status_code == 404:
            return {"error": f"Template '{template_id}' not found in {repo}@{branch}"}

        if response.status_code == 403:
            # Check if rate limited
            if "rate limit" in response.text.lower():
                return {"error": "GitHub API rate limit exceeded. Try again later."}
            return {"error": f"GitHub API access denied: {response.text}"}

        response.raise_for_status()

        items = response.json()

        # Handle case where path is a file, not a directory
        if isinstance(items, dict):
            return {"error": f"'{template_id}' is a file, not a template directory"}

        # Filter to deployable files
        files = [
            FileToFetch(
                name=item["name"],
                download_url=item["download_url"],
            )
            for item in items
            if item["type"] == "file"
            and item["name"] not in EXCLUDED_FILES
            and not item["name"].startswith(".")
        ]

        return {"files": files}

    except httpx.TimeoutException:
        return {"error": "GitHub API request timed out"}
    except Exception as e:
        return {"error": f"GitHub API error: {str(e)}"}


@function(timeout=30, retries=Retries(max_retries=3))
def fetch_file(download_url: str) -> Optional[str]:
    """
    Fetch a single file from GitHub raw URL.

    Each invocation runs in its own container. When called via map(),
    N files are fetched by N parallel containers simultaneously.

    Uses raw.githubusercontent.com which has generous rate limits
    (separate from the GitHub API limits).
    """
    try:
        response = httpx.get(
            download_url,
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "tensorlake-template-deployer"},
        )
        response.raise_for_status()
        return response.text
    except Exception:
        return None


@function(timeout=60)
def generate_manifest(main_py_content: str, app_name: str) -> dict:
    """
    Generate application manifest from Python source code.

    Uses Tensorlake SDK internals - the exact same code path as
    the `tensorlake deploy` CLI command:

    1. Write source to temp file
    2. load_code() executes decorators, registers functions
    3. create_application_manifest() builds the manifest
    4. Serialize to JSON

    No pre-generated manifests needed - always fresh from source.
    """
    from tensorlake.cli.deploy import load_code, get_functions
    from tensorlake.applications.remote.manifests.application import create_application_manifest
    from tensorlake.applications import registry

    with tempfile.TemporaryDirectory() as tmpdir:
        main_path = os.path.join(tmpdir, "main.py")

        with open(main_path, "w") as f:
            f.write(main_py_content)

        try:
            # Clear registry to ensure isolation between deployments
            registry._function_registry.clear()
            registry._class_registry.clear()
            registry._decorators.clear()

            # Load code - this executes decorators and registers functions
            load_code(main_path)

            # Get registered functions
            functions = get_functions()

            if not functions:
                return {"error": "No @function decorated functions found in main.py"}

            # Find the @application decorated function (entrypoint)
            app_func = None
            for func in functions:
                if func._application_config is not None:
                    app_func = func
                    break

            if app_func is None:
                return {"error": "No @application decorated function found in main.py"}

            # Generate manifest using SDK
            manifest = create_application_manifest(app_func, functions)
            manifest.name = app_name

            return {"manifest_json": manifest.model_dump_json()}

        except Exception as e:
            return {"error": f"Failed to parse source code: {str(e)}"}


@function(timeout=60, secrets=["TENSORLAKE_DEPLOYER_API_KEY"])
def deploy_to_namespace(manifest_json: str, code_zip: bytes, namespace: str) -> dict:
    """
    Deploy application to target namespace via Tensorlake API.

    Uses SDK's APIClient - the same client that powers the CLI.
    """
    from tensorlake.applications.remote.api_client import APIClient

    api_key = os.environ.get("TENSORLAKE_DEPLOYER_API_KEY")
    if not api_key:
        return {
            "success": False,
            "error": "TENSORLAKE_DEPLOYER_API_KEY secret not configured"
        }

    client = APIClient(api_key=api_key, namespace=namespace)

    try:
        client.upsert_application(
            manifest_json=manifest_json,
            code_zip=code_zip,
            upgrade_running_requests=True,
        )
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        client.close()


def create_zip(files: Dict[str, str]) -> bytes:
    """Create a zip file in memory from files dictionary."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    return buffer.getvalue()
