"""Verify immutable upload bytes before exposing any package to ingestion."""
import hashlib
import json
from pathlib import Path
import re


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value, *, sort=False):
    return json.dumps(value, ensure_ascii=False, sort_keys=sort,
                      separators=(",", ":"), allow_nan=False).encode()


def mapping_hash(mapping):
    # Exporter uses Python's default separators for this hash, unlike raw_sha256.
    return sha(json.dumps(mapping, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())


def loads(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_source(root, relative, pattern):
    require(isinstance(relative, str) and re.fullmatch(pattern, relative), "Invalid source path")
    target = root / relative
    require(target.resolve().is_relative_to(root.resolve()), "Source path escapes account")
    require(not any(p.is_symlink() for p in [target, *target.parents] if p != root.parent),
            "Symlink in source path")
    return target.read_bytes()


def verify(account_root, commit_path):
    root = Path(account_root).absolute()
    commit_path = Path(commit_path).absolute()
    require(commit_path.parent == root / "commits", "Commit outside account commits")
    data = read_source(root, "commits/" + commit_path.name, r"commits/[0-9a-f]{64}\.commit\.json")
    require(commit_path.name == sha(data) + ".commit.json", "Commit filename checksum mismatch")
    commit = loads(data)
    require(type(commit.get("schema_version")) is int and commit["schema_version"] == 1
            and commit.get("type") == "chatgpt-upload-commit",
            "Unsupported commit")
    require(commit.get("account_key") == root.name, "Commit account mismatch")
    catalog_data = read_source(root, commit.get("catalog"), r"catalogs/[0-9a-f]{64}\.json")
    require(sha(catalog_data) == commit.get("catalog_sha256") == Path(commit["catalog"]).stem,
            "Catalog checksum mismatch")
    catalog = loads(catalog_data)
    require(type(catalog.get("schema_version")) is int and catalog["schema_version"] == 1
            and catalog.get("account_key") == root.name,
            "Catalog identity/schema mismatch")
    require(isinstance(commit.get("objects"), list), "Invalid objects list")
    packages = {}
    for entry in commit["objects"]:
        name = entry["path"]
        require(name not in packages, "Duplicate object path")
        body = read_source(root, name, r"objects/[A-Za-z0-9_-]{1,100}/[0-9a-f]{64}\.json")
        require(type(entry.get("size")) is int and len(body) == entry["size"]
                and sha(body) == entry.get("sha256"), "Object byte checksum mismatch: " + name)
        package = loads(body)
        require(type(package.get("schema_version")) is int and package["schema_version"] == 1
                and package.get("source") == "chatgpt"
                and package.get("untrusted_data") is True, "Unsupported package")
        require(package.get("conversation_id") == Path(name).parent.name, "Conversation identity mismatch")
        raw = package["raw"]
        require(isinstance(raw, dict), "Invalid raw payload")
        require(sha(encoded(raw)) == package.get("raw_sha256") == Path(name).stem,
                "Raw payload checksum mismatch")
        require(mapping_hash(raw.get("mapping")) == package.get("content_sha256"),
                "Mapping checksum mismatch")
        require(raw.get("conversation_id", package["conversation_id"]) == package["conversation_id"],
                "Raw conversation mismatch")
        packages[name] = {"package": package, "byte_sha256": sha(body)}
    require(type(commit.get("object_count")) is int and len(packages) == commit["object_count"],
            "Object count mismatch")
    require(isinstance(catalog.get("conversations"), list), "Invalid catalog conversations")
    seen = set()
    for entry in catalog["conversations"]:
        identity = entry["id"]
        require(isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity)
                and identity not in seen, "Invalid or duplicate catalog conversation")
        seen.add(identity)
        require(entry.get("body_status") in ("current", "stale", "not_fetched"), "Unknown body status")
        name = entry.get("object")
        require(entry["body_status"] == "not_fetched" or bool(name), "Missing body reference")
        if name:
            require(name in packages and name == f"objects/{identity}/{entry['raw_sha256']}.json",
                    "Missing or mismatched catalog object")
            require(entry.get("content_sha256") == packages[name]["package"]["content_sha256"],
                    "Catalog content hash mismatch")
    require(type(catalog.get("listing_complete")) is bool and type(catalog.get("bodies_complete")) is bool,
            "Invalid catalog completeness")
    body_count = sum(bool(e.get("object")) for e in catalog["conversations"])
    require(type(catalog.get("body_count")) is int and catalog["body_count"] == body_count,
            "Catalog body count mismatch")
    require(catalog["bodies_complete"] == all(e["body_status"] == "current" for e in catalog["conversations"]),
            "Catalog completeness mismatch")
    return {"account_key": root.name, "commit_sha256": sha(data), "commit_path": str(commit_path),
            "catalog_sha256": sha(catalog_data), "catalog_path": str(root / commit["catalog"]),
            "commit": commit, "catalog": catalog, "packages": packages}
