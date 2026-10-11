from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import paths

DEFAULT_BASE_URL = "https://scripting.replay.re/"
CACHE_TTL_S = 3600
USER_AGENT = "blackice-mcp"

EXPECTED_KEYS = ("realm", "entries")


@dataclass
class ApiEntry:
    kind: str
    realm: str
    class_name: str
    namespace: str
    name: str
    qualified: str
    is_static: bool
    url: str
    signature: str | None
    result: str | None
    parameters: list[dict[str, Any]] | None
    base: str | None
    inherited_from: str | None = None

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> ApiEntry:
        return cls(
            kind=data["kind"],
            realm=data["realm"],
            class_name=data.get("class", ""),
            namespace=data.get("namespace", ""),
            name=data["name"],
            qualified=data["qualified"],
            is_static=data.get("is_static", False),
            url=data.get("url", ""),
            signature=data.get("signature"),
            result=data.get("result"),
            parameters=data.get("parameters"),
            base=data.get("base"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "kind": self.kind,
            "realm": self.realm,
            "class": self.class_name,
            "qualified": self.qualified,
            "signature": self.signature,
            "result": self.result,
            "parameters": self.parameters,
            "is_static": self.is_static,
            "url": self.url,
        }
        if self.inherited_from is not None:
            payload["inherited_from"] = self.inherited_from
            payload["defined_on"] = self.class_name
            payload["note"] = (
                f"declared on {self.class_name}, reachable from {self.inherited_from}"
            )
        return payload


class ApiDocs:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, cache_dir: Path | None = None) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self._cache_dir = cache_dir or paths.cache_dir()
        self._indexes: dict[str, list[ApiEntry]] = {}

    def _cache_path(self, realm: str) -> Path:
        return self._cache_dir / f"api-index-{realm}.json"

    @staticmethod
    def _validate(body: str, source: str) -> dict[str, Any]:
        # The docs host answers a missing path with a 200 HTML page, which must not reach the cache
        if body.lstrip()[:1] != "{":
            raise RuntimeError(
                f"{source} is not JSON (starts with {body.lstrip()[:40]!r}). "
                "The docs host may be serving a fallback page; check that the API index is deployed."
            )

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{source} is not valid JSON: {exc}") from exc

        if not isinstance(parsed, dict) or not all(key in parsed for key in EXPECTED_KEYS):
            raise RuntimeError(f"{source} is missing {EXPECTED_KEYS}; not an API index")

        return parsed

    def _load_raw(self, realm: str, refresh: bool = False) -> dict[str, Any]:
        cached = self._cache_path(realm)

        if cached.is_file():
            age = time.time() - cached.stat().st_mtime
            if not refresh and age < CACHE_TTL_S:
                return self._validate(cached.read_text(), f"cached {cached}")

        url = f"{self.base_url}api-index-{realm}.json.gz"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if cached.is_file():
                return self._validate(cached.read_text(), f"cached {cached}")
            raise RuntimeError(f"could not fetch {url} and no cached copy exists: {exc}") from exc

        try:
            body = gzip.decompress(raw).decode()
        except (gzip.BadGzipFile, EOFError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"{url} did not return a gzip stream ({exc}). The docs host may be serving a "
                f"fallback page; check that the API index is deployed. "
                f"First bytes: {raw[:40]!r}"
            ) from exc

        parsed = self._validate(body, url)

        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(body)
        return parsed

    def _by_qualified(self, realm: str) -> dict[str, ApiEntry]:
        return {entry.qualified.lower(): entry for entry in self.entries(realm)}

    def base_chain(self, class_name: str, realm: str = "client") -> list[str]:
        by_name = self._by_qualified(realm)
        chain: list[str] = []
        current = class_name

        while current and current.lower() in by_name and len(chain) < 16:
            entry = by_name[current.lower()]
            chain.append(entry.qualified)
            current = entry.base or ""

        return chain

    def members_of(self, class_name: str, realm: str = "client") -> list[ApiEntry]:
        chain = self.base_chain(class_name, realm)
        wanted = set(chain)
        resolved = chain[0] if chain else class_name

        found: list[ApiEntry] = []
        for entry in self.entries(realm):
            if entry.kind not in ("function", "property"):
                continue
            if entry.class_name not in wanted:
                continue

            if entry.class_name != resolved:
                entry = replace(entry, inherited_from=resolved)

            found.append(entry)

        return found

    def entries(self, realm: str, refresh: bool = False) -> list[ApiEntry]:
        if realm not in self._indexes or refresh:
            raw = self._load_raw(realm, refresh)
            self._indexes[realm] = [ApiEntry.from_wire(e) for e in raw["entries"]]

        return self._indexes[realm]

    def search(
        self,
        query: str,
        realm: str = "client",
        kind: str | None = None,
        class_name: str | None = None,
        limit: int = 40,
        refresh: bool = False,
    ) -> list[ApiEntry]:
        needle = query.lower().strip()
        allowed_classes = set(self.base_chain(class_name, realm)) if class_name else None

        scored: list[tuple[int, str, ApiEntry]] = []

        for entry in self.entries(realm, refresh):
            if kind and entry.kind != kind:
                continue
            if allowed_classes is not None and entry.class_name not in allowed_classes:
                continue

            if not needle:
                scored.append((3, entry.qualified, entry))
                continue

            name = entry.name.lower()
            qualified = entry.qualified.lower()

            if name == needle:
                rank = 0
            elif name.startswith(needle):
                rank = 1
            elif needle in name:
                rank = 2
            elif needle in qualified:
                rank = 3
            else:
                continue

            scored.append((rank, qualified, entry))

        scored.sort(key=lambda item: (item[0], item[1]))
        return [entry for _, _, entry in scored[:limit]]

    def describe(self, qualified: str, realm: str = "client", refresh: bool = False) -> list[ApiEntry]:
        owner, _, member = qualified.strip().rpartition(".")

        if owner:
            exact = [e for e in self.members_of(owner, realm) if e.name.lower() == member.lower()]
            if exact:
                return exact

        return self.search(member, realm=realm, limit=20, refresh=refresh)
