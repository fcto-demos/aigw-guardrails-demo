#!/usr/bin/env python3
"""Finds which product images to run from the external registry set in
secrets/registry.env. For each product, it takes the newest update of the
version in demo.env: for 0.16.0, the newest 0.16.0.N.

It only reads the registry's list of versions: Docker downloads the images
later, when it starts them. It prints one NAME=image line per image, and
nothing when USE_EXTERNAL_REGISTRY is not true.
"""
import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

from target import ROOT, DEMO_ENV, read_env_file

REGISTRY_FILE = ROOT / "secrets" / "registry.env"

# Each product: its image name, the variable to set, and its version in demo.env.
PRODUCTS = [
    ("platform-api", "PLATFORM_API_IMAGE", "PLATFORM_API_VERSION"),
    ("ai-workspace", "AI_WORKSPACE_IMAGE", "AI_WORKSPACE_VERSION"),
    ("api-portal", "API_PORTAL_IMAGE", "API_PORTAL_VERSION"),
]
# The gateway's three base images. They must be the same update.
GATEWAY_IMAGES = [
    ("gateway-builder", "GATEWAY_BUILDER_IMAGE"),
    ("gateway-controller", "GATEWAY_CONTROLLER_IMAGE"),
    ("gateway-runtime", "GATEWAY_RUNTIME_IMAGE"),
]

# Some registries only answer clients that look like Docker.
USER_AGENT = "docker/27.3.1 (tag list)"


def http_get(url: str, auth: str = None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if auth:
        headers["Authorization"] = auth
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null"), resp.headers
    except urllib.error.HTTPError as err:
        return err.code, None, err.headers


def fetch_tags(host: str, repository: str, basic: str) -> list:
    """The list of versions (tags) of an image on the registry."""
    status, _, headers = http_get(f"https://{host}/v2/")
    challenge = headers.get("WWW-Authenticate", "") if status == 401 else ""
    auth = basic
    if challenge.lower().startswith("bearer"):
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        status, body, _ = http_get(f"{params['realm']}?service={params.get('service', '')}"
                                   f"&scope=repository:{repository}:pull", basic)
        if status != 200:
            sys.exit(f"{host}: the token was refused ({status}): check {REGISTRY_FILE}")
        auth = "Bearer " + (body.get("token") or body.get("access_token"))
    status, body, _ = http_get(f"https://{host}/v2/{repository}/tags/list", auth)
    if status != 200:
        sys.exit(f"{host}/{repository}: tags not readable ({status})")
    return body.get("tags") or []


def updates(version: str, tags: list) -> list:
    """The update numbers of a version in the tags: 0.16.0.4 gives 4. Gives
    [-1] when only the version itself (0.16.0) is there."""
    found = [int(t.rsplit(".", 1)[1]) for t in tags if re.fullmatch(re.escape(version) + r"\.\d+", t)]
    return found or ([-1] if version in tags else [])


def tag(version: str, update: int) -> str:
    return version if update < 0 else f"{version}.{update}"


def main():
    conf = read_env_file(REGISTRY_FILE) if REGISTRY_FILE.exists() else {}
    if conf.get("USE_EXTERNAL_REGISTRY", "").lower() != "true":
        return
    url = conf.get("EXTERNAL_REGISTRY_URL", "").removeprefix("https://").rstrip("/")
    if not url:
        sys.exit(f"USE_EXTERNAL_REGISTRY is true but EXTERNAL_REGISTRY_URL is empty in {REGISTRY_FILE}")
    host, _, path = url.partition("/")
    username, token = conf.get("EXTERNAL_REGISTRY_USERNAME", ""), conf.get("EXTERNAL_REGISTRY_TOKEN", "")
    login = subprocess.run(["docker", "login", host, "-u", username, "--password-stdin"],
                           input=token, capture_output=True, text=True)
    if login.returncode != 0:
        sys.exit(f"docker login {host} failed: check the username and token in {REGISTRY_FILE}")
    basic = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()

    images = {}
    for product, variable, setting in PRODUCTS:
        version = DEMO_ENV[setting]
        found = updates(version, fetch_tags(host, f"{path}/{product}".lstrip("/"), basic))
        if not found:
            sys.exit(f"{url}/{product}: no {version} image")
        images[variable] = f"{url}/{product}:{tag(version, max(found))}"

    version = DEMO_ENV["AI_GATEWAY_VERSION"]
    common = set.intersection(*(set(updates(version, fetch_tags(host, f"{path}/{name}".lstrip("/"), basic)))
                                for name, _ in GATEWAY_IMAGES))
    if not common:
        sys.exit(f"{url}: no gateway {version} update with all three images")
    for name, variable in GATEWAY_IMAGES:
        images[variable] = f"{url}/{name}:{tag(version, max(common))}"

    for variable, image in images.items():
        print(f"{variable}={image}")


if __name__ == "__main__":
    main()
