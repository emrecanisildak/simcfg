"""Çalışma alanı: paketleri yükler, dokümanları indeksler, şema doğrulaması yapar, kalıtımı çözer."""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .diagnostics import Diagnostics, jp
from .merge import MergeError, merge, record_provenance
from .units import UnitCatalog

WORKSPACE_FILE = "simcfg.workspace.json"
PACKAGE_FILE = "simpkg.json"
API_VERSION = "simcfg/v1"

# Kalıtımda üzerine yazılmak yerine birleşen (kümülatif) liste alanları: kind -> spec anahtarları.
CUMULATIVE_KEYS: dict[str, tuple[str, ...]] = {"SystemType": ("emits",), "PlatformType": ("emits",)}

try:  # jsonschema opsiyonel: yoksa yapısal doğrulama atlanır, semantik doğrulama yine çalışır.
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import best_match
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    HAVE_JSONSCHEMA = False


@dataclass
class Document:
    raw: dict
    file: Path
    package: str
    index: int | None = None  # dizi dosyalarında konum

    @property
    def id(self) -> str:
        return self.raw.get("id", "")

    @property
    def kind(self) -> str:
        return self.raw.get("kind", "")

    @property
    def extends(self) -> list[str]:
        e = self.raw.get("extends")
        if e is None:
            return []
        return [e] if isinstance(e, str) else list(e)

    @property
    def abstract(self) -> bool:
        return bool(self.raw.get("abstract", False))

    @property
    def spec(self) -> dict:
        return self.raw.get("spec") or {}

    @property
    def meta(self) -> dict:
        return self.raw.get("meta") or {}

    @property
    def location(self) -> str:
        return f"{self.file}" + (f"[{self.index}]" if self.index is not None else "")


@dataclass
class Resolved:
    """Kalıtımı çözülmüş doküman."""

    id: str
    kind: str
    abstract: bool
    linear: list[str]  # birleştirme sırası (atadan kendine)
    spec: dict
    meta: dict
    doc: Document
    provenance: dict[str, str] = field(default_factory=dict)

    def is_a(self, other: str) -> bool:
        return other in self.linear

    def as_document(self) -> dict:
        out = {"apiVersion": API_VERSION, "kind": self.kind, "id": self.id}
        for k in ("version", "extends", "abstract", "deprecated"):
            if k in self.doc.raw:
                out[k] = self.doc.raw[k]
        out["meta"] = self.meta
        out["spec"] = self.spec
        return out


class Workspace:
    def __init__(self, root: Path, diags: Diagnostics | None = None,
                 overrides: dict[tuple[str, int | None], dict] | None = None) -> None:
        """overrides: (dosya yolu (köke göre, posix), dizi indeksi) -> ham doküman. Editörün kaydedilmemiş taslakları için."""
        self.root = Path(root).resolve()
        self.overrides = overrides or {}
        self.d = diags or Diagnostics()
        self.docs: dict[str, Document] = {}
        self.packages: dict[str, Document] = {}
        self.kinds: dict[str, dict] = {}
        self.units = UnitCatalog()
        self.schema_dir: Path = self.root / "schemas" / "v1"
        self._schemas: dict[str, dict] = {}
        self._validators: dict[str, Any] = {}
        self._registry = None
        self._resolved: dict[str, Resolved | None] = {}
        self._resolving: set[str] = set()

    # ------------------------------------------------------------------ yükleme
    @classmethod
    def find_root(cls, start: Path) -> Path | None:
        cur = Path(start).resolve()
        for p in [cur, *cur.parents]:
            if (p / WORKSPACE_FILE).exists():
                return p
        return None

    def load(self) -> "Workspace":
        cfg_path = self.root / WORKSPACE_FILE
        cfg = self._read_json(cfg_path)
        if not isinstance(cfg, dict):
            self.d.error("workspace.invalid", f"{WORKSPACE_FILE} okunamadı", file=str(cfg_path))
            return self
        self.schema_dir = self.root / cfg.get("schemas", "schemas/v1")
        self._load_schemas()
        for pkg_dir in cfg.get("packages", []):
            self._load_package(self.root / pkg_dir)
        self._check_dependencies()
        for doc in self.by_kind("UnitCatalog"):
            for problem in self.units.add_spec(doc.spec):
                self.d.error("units.invalid", problem, doc=doc.id)
        return self

    def _read_json(self, path: Path) -> Any:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            self.d.error("json.parse", f"satır {e.lineno}, sütun {e.colno}: {e.msg}", file=str(self.rel(path)))
        except OSError as e:
            self.d.error("file.read", str(e), file=str(self.rel(path)))
        return None

    def rel(self, path: Path) -> Path:
        try:
            return Path(path).resolve().relative_to(self.root)
        except ValueError:
            return Path(path)

    def _load_schemas(self) -> None:
        reg = self._read_json(self.schema_dir / "registry.json") or {}
        self.kinds = reg.get("kinds", {})
        if not HAVE_JSONSCHEMA:
            self.d.warning("schema.unavailable", "jsonschema kurulu değil; yapısal şema doğrulaması atlandı (pip install jsonschema)")
            return
        resources = []
        for p in sorted(self.schema_dir.glob("*.schema.json")):
            s = self._read_json(p)
            if isinstance(s, dict) and "$id" in s:
                self._schemas[p.name] = s
                resources.append((s["$id"], Resource.from_contents(s, default_specification=DRAFT202012)))
        self._registry = Registry().with_resources(resources)

    def _validator(self, schema_file: str):
        if not HAVE_JSONSCHEMA or schema_file not in self._schemas:
            return None
        if schema_file not in self._validators:
            self._validators[schema_file] = Draft202012Validator(self._schemas[schema_file], registry=self._registry)
        return self._validators[schema_file]

    def schema_validate(self, data: dict, schema_file: str, doc_id: str | None = None) -> bool:
        v = self._validator(schema_file)
        if v is None:
            return True
        ok = True
        for err in sorted(v.iter_errors(data), key=lambda e: list(e.absolute_path)):
            if err.context:
                err = best_match(err.context) or err
            path = ".".join(str(p) if not isinstance(p, int) else f"[{p}]" for p in err.absolute_path).replace(".[", "[")
            msg = err.message if len(err.message) < 300 else err.message[:300] + "…"
            self.d.error("schema", msg, path=path or None, doc=doc_id)
            ok = False
        return ok

    def _load_package(self, pkg_dir: Path) -> None:
        manifest_path = pkg_dir / PACKAGE_FILE
        manifest = self._read_json(manifest_path)
        if not isinstance(manifest, dict):
            self.d.error("package.missing", f"{PACKAGE_FILE} bulunamadı/okunamadı", file=str(self.rel(manifest_path)))
            return
        pkg = Document(manifest, self.rel(manifest_path), manifest.get("id", "?"))
        with self.d.scope(doc=pkg.id, file=str(pkg.file)):
            self.schema_validate(manifest, "package.schema.json", pkg.id)
            if pkg.kind != "Package":
                self.d.error("package.kind", "manifest kind 'Package' olmalı")
                return
            if pkg.id in self.packages:
                self.d.error("package.duplicate", f"'{pkg.id}' paketi birden fazla kez yüklendi")
                return
        self.packages[pkg.id] = pkg
        spec = pkg.spec
        includes = spec.get("include") or ["**/*.json"]
        excludes = spec.get("exclude") or []
        files = set()
        for pattern in includes:
            files.update(p for p in pkg_dir.glob(pattern) if p.is_file())
        for f in sorted(files):
            rel = f.relative_to(pkg_dir).as_posix()
            if f.name == PACKAGE_FILE or any(fnmatch.fnmatch(rel, ex) for ex in excludes):
                continue
            data = self._read_json(f)
            if data is None:
                continue
            items = data if isinstance(data, list) else [data]
            for i, raw in enumerate(items):
                idx = i if isinstance(data, list) else None
                raw = self.overrides.get((self.rel(f).as_posix(), idx), raw)
                self._add_doc(Document(raw, self.rel(f), pkg.id, idx))

    def _add_doc(self, doc: Document) -> None:
        with self.d.scope(doc=doc.id or None, file=doc.location):
            if not isinstance(doc.raw, dict):
                self.d.error("doc.invalid", "doküman bir JSON nesnesi olmalı")
                return
            if not self.schema_validate(doc.raw, "document.schema.json", doc.id or None):
                if not doc.id or not doc.kind:
                    return
            if doc.raw.get("apiVersion") != API_VERSION:
                self.d.error("doc.apiVersion", f"desteklenmeyen apiVersion '{doc.raw.get('apiVersion')}' (beklenen {API_VERSION})")
                return
            if doc.kind not in self.kinds:
                self.d.error("doc.kind", f"bilinmeyen kind '{doc.kind}'. Kayıtlı türler: {', '.join(sorted(self.kinds))}")
                return
            if doc.kind == "Package":
                self.d.error("doc.kind", "Package dokümanı sadece simpkg.json olabilir")
                return
            if not doc.id.startswith(doc.package + "."):
                self.d.error("doc.namespace", f"'{doc.id}' kimliği paket ad alanıyla ('{doc.package}.') başlamalı")
            if doc.id in self.docs:
                other = self.docs[doc.id]
                self.d.error("doc.duplicate", f"'{doc.id}' zaten tanımlı: {other.location}")
                return
        self.docs[doc.id] = doc

    def _check_dependencies(self) -> None:
        for pkg in self.packages.values():
            for dep, rng in (pkg.spec.get("dependencies") or {}).items():
                other = self.packages.get(dep)
                if other is None:
                    self.d.error("package.dependency", f"bağımlılık '{dep}' çalışma alanında yok", doc=pkg.id)
                elif not version_satisfies(other.raw.get("version", "0.0.0"), rng):
                    self.d.error("package.dependency", f"'{dep}' sürümü {other.raw.get('version')} '{rng}' aralığını karşılamıyor", doc=pkg.id)
        # dokümanlar sadece kendi paketlerine ve bağımlılıklarına referans verebilir (resolve sırasında kontrol edilir)

    def visible_packages(self, pkg: str) -> set[str]:
        seen, stack = set(), [pkg]
        while stack:
            p = stack.pop()
            if p in seen or p not in self.packages:
                continue
            seen.add(p)
            stack.extend((self.packages[p].spec.get("dependencies") or {}).keys())
        return seen

    # ------------------------------------------------------------------ sorgu
    def get(self, doc_id: str) -> Document | None:
        return self.docs.get(doc_id)

    def by_kind(self, kind: str) -> list[Document]:
        return [d for d in self.docs.values() if d.kind == kind]

    # ------------------------------------------------------------------ kalıtım
    def linearize(self, doc_id: str, _stack: tuple[str, ...] = ()) -> list[str] | None:
        """DFS post-order, soldan sağa, ilk görülen korunur. Ortak atalar bir kez uygulanır."""
        if doc_id in _stack:
            cycle = " -> ".join([*_stack[_stack.index(doc_id):], doc_id])
            self.d.error("extends.cycle", f"kalıtım döngüsü: {cycle}", doc=doc_id)
            return None
        doc = self.docs.get(doc_id)
        if doc is None:
            return None
        out: list[str] = []
        for parent in doc.extends:
            pdoc = self.docs.get(parent)
            if pdoc is None:
                self.d.error("extends.unknown", f"bilinmeyen üst doküman '{parent}'", doc=doc_id, path="extends")
                continue
            if pdoc.kind != doc.kind:
                self.d.error("extends.kind", f"'{parent}' türü {pdoc.kind}; {doc.kind} ondan türetilemez", doc=doc_id, path="extends")
                continue
            if pdoc.package not in self.visible_packages(doc.package):
                self.d.error("extends.visibility", f"'{parent}' paketi '{pdoc.package}' bağımlılıklarda yok", doc=doc_id, path="extends")
            sub = self.linearize(parent, (*_stack, doc_id))
            if sub is None:
                return None
            for x in sub:
                if x not in out:
                    out.append(x)
        out.append(doc_id)
        return out

    def resolve(self, doc_id: str) -> Resolved | None:
        if doc_id in self._resolved:
            return self._resolved[doc_id]
        doc = self.docs.get(doc_id)
        if doc is None:
            return None
        linear = self.linearize(doc_id)
        if linear is None:
            self._resolved[doc_id] = None
            return None
        spec: Any = {}
        prov: dict[str, str] = {}
        for layer_id in linear:
            layer = self.docs[layer_id]
            try:
                spec = merge(spec, layer.spec)
            except MergeError as e:
                self.d.error("merge", str(e), doc=layer_id)
            record_provenance(layer.spec, layer_id, prov)
        for key in CUMULATIVE_KEYS.get(doc.kind, ()):
            acc: list = []
            for layer_id in linear:
                lv = self.docs[layer_id].spec.get(key)
                if isinstance(lv, dict) and "$replace" in lv:
                    acc = list(lv["$replace"])
                elif isinstance(lv, dict) and ("$append" in lv or "$prepend" in lv):
                    acc += [x for x in (lv.get("$append") or lv.get("$prepend") or []) if x not in acc]
                elif isinstance(lv, list):
                    acc += [x for x in lv if x not in acc]
            if acc or key in spec:
                spec[key] = acc
        r = Resolved(doc_id, doc.kind, doc.abstract, linear, spec, doc.meta, doc, prov)
        self._resolved[doc_id] = r
        return r

    def resolve_kind(self, doc_id: Any, kind: str, path: str | None = None, *, allow_abstract: bool = True,
                     what: str | None = None) -> Resolved | None:
        """Referansı çöz ve türünü kontrol et (tanı üretir)."""
        if not isinstance(doc_id, str):
            self.d.error("ref.type", f"{what or kind} kimliği metin olmalı", path)
            return None
        r = self.resolve(doc_id)
        if r is None:
            if doc_id not in self.docs:
                self.d.error("ref.unknown", f"bilinmeyen {what or kind} '{doc_id}'{suggest(doc_id, [d.id for d in self.by_kind(kind)])}", path)
            return None
        if r.kind != kind:
            self.d.error("ref.kind", f"'{doc_id}' bir {r.kind}; {kind} bekleniyordu", path)
            return None
        if r.abstract and not allow_abstract:
            self.d.error("ref.abstract", f"'{doc_id}' soyut (abstract); doğrudan kullanılamaz, türetin", path)
        return r

    def is_a(self, doc_id: str | None, base_id: str | None) -> bool:
        if not doc_id or not base_id:
            return False
        r = self.resolve(doc_id)
        return bool(r and r.is_a(base_id))

    def children(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for d in self.docs.values():
            for p in d.extends:
                out.setdefault(p, []).append(d.id)
        return out


# ---------------------------------------------------------------------- yardımcılar
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _v(s: str) -> tuple[int, int, int]:
    m = _SEMVER.match(s.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def version_satisfies(version: str, rng: str) -> bool:
    rng = rng.strip()
    v = _v(version)
    if rng in ("*", ""):
        return True
    if rng.startswith("^"):
        b = _v(rng[1:])
        return v >= b and v[0] == b[0]
    if rng.startswith("~"):
        b = _v(rng[1:])
        return v >= b and v[:2] == b[:2]
    if rng.startswith(">="):
        return v >= _v(rng[2:])
    return v == _v(rng)


def suggest(name: str, candidates: list[str] | Any) -> str:
    import difflib

    m = difflib.get_close_matches(str(name), list(candidates), n=1, cutoff=0.6)
    return f" (bunu mu demek istediniz: '{m[0]}'?)" if m else ""


__all__ = ["Workspace", "Document", "Resolved", "version_satisfies", "suggest", "jp"]
