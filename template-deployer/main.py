"""
Template Deployer - Deploy Tensorlake applications from GitHub.

This application demonstrates:
- GitHub API integration for dynamic file discovery
- Parallel file fetching using map() - N files = N containers
- CLI-based deployment (same code path as manual deployment)
- Multi-stage orchestration with proper error handling

No hardcoded file lists. The deployer discovers what files exist
in a template directory and fetches them all in parallel.
"""

import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional, List, Dict

import httpx
from tensorlake.applications import application, function, Retries, Image

# Custom image with dependencies needed to parse template source code
deployer_image = Image().run("pip install httpx beautifulsoup4")


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
@function(timeout=180)
def deploy_template(request: dict) -> dict:
    """
    Deploy a template from a GitHub repository.

    Pipeline:
    1. Discover files via GitHub API (no hardcoded file list)
    2. Fetch all files in parallel using map()
    3. Deploy using tensorlake CLI

    This is Tensorlake deploying Tensorlake - the same code path
    as the CLI, orchestrated as a distributed application.
    """
    # Parse request dict into dataclass
    req = DeployRequest(
        template_id=request["template_id"],
        target_namespace=request["target_namespace"],
        app_name=request["app_name"],
        github_repo=request.get("github_repo", "tensorlakeai/examples"),
        github_branch=request.get("github_branch", "main"),
    )

    # Stage 1: Discover files in the template directory
    discovery = discover_template_files(
        repo=req.github_repo,
        branch=req.github_branch,
        template_id=req.template_id,
    )

    if discovery.get("error"):
        return {"success": False, "error": discovery["error"]}

    files_to_fetch: List[FileToFetch] = discovery["files"]

    if not files_to_fetch:
        return {
            "success": False,
            "error": f"Template '{req.template_id}' is empty or not found"
        }

    # Validate main.py exists
    if not any(f.name == "main.py" for f in files_to_fetch):
        return {
            "success": False,
            "error": f"Template '{req.template_id}' is missing main.py"
        }

    # Stage 2: Fetch all files in parallel using map()
    # Each file download runs in its own container
    download_urls = [f.download_url for f in files_to_fetch]
    file_contents = fetch_file.map(download_urls)

    # Build files dictionary
    files: Dict[str, str] = {}
    for file_meta, content in zip(files_to_fetch, file_contents):
        if content is None:
            return {
                "success": False,
                "error": f"Failed to download {file_meta.name}"
            }
        files[file_meta.name] = content

    # Stage 3: Deploy using tensorlake CLI
    deploy_result = deploy_via_cli(
        files=files,
        namespace=req.target_namespace,
    )

    if not deploy_result["success"]:
        return {
            "success": False,
            "error": deploy_result.get("error"),
            "stderr": deploy_result.get("stderr"),
            "stdout": deploy_result.get("stdout"),
        }

    return {
        "success": True,
        "app_name": req.app_name,
        "invoke_url": f"https://api.tensorlake.ai/applications/{req.app_name}",
        "files_deployed": list(files.keys()),
        "deploy_output": deploy_result.get("stdout"),
    }


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


@function(timeout=120, image=deployer_image, secrets=["TENSORLAKE_DEPLOYER_API_KEY"])
def deploy_via_cli(files: Dict[str, str], namespace: str) -> dict:
    """
    Deploy application using tensorlake CLI.

    Writes files to temp directory and runs `tensorlake deploy`.
    This uses the exact same code path as manual CLI deployment.
    """
    import subprocess

    api_key = os.environ.get("TENSORLAKE_DEPLOYER_API_KEY")
    if not api_key:
        return {
            "success": False,
            "error": "TENSORLAKE_DEPLOYER_API_KEY secret not configured"
        }

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write all files to temp directory
        for filename, content in files.items():
            filepath = os.path.join(tmpdir, filename)
            with open(filepath, "w") as f:
                f.write(content)

        main_path = os.path.join(tmpdir, "main.py")

        try:
            # Run tensorlake deploy with API key via environment variable
            env = os.environ.copy()
            env["TENSORLAKE_API_KEY"] = api_key

            result = subprocess.run(
                [
                    "tensorlake",
                    "--namespace", namespace,
                    "deploy", main_path,
                ],
                capture_output=True,
                text=True,
                timeout=90,
                cwd=tmpdir,
                env=env,
            )

            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"tensorlake deploy failed",
                    "stderr": result.stderr,
                    "stdout": result.stdout,
                }

            return {
                "success": True,
                "stdout": result.stdout,
            }

        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Deployment timed out"}
        except Exception as e:
            import traceback
            return {
                "success": False,
                "error": f"Deployment failed: {str(e)}",
                "traceback": traceback.format_exc()
            }


