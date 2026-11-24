"""simcfg komut satırı arayüzü."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compiler import Builder
from .diagnostics import Diagnostics
from .editor import build_catalog, derive, describe
from .validate import Validator
from .workspace import WORKSPACE_FILE, Workspace


def _load(args) -> tuple[Workspace, Diagnostics]:
    root = Path(args.root) if args.root else Workspace.find_root(Path.cwd())
    if root is None:
        print(f"{WORKSPACE_FILE} bulunamadı (--root ile belirtin)", file=sys.stderr)
        sys.exit(2)
    d = Diagnostics()
    ws = Workspace(root, d).load()
    return ws, d


def _report(d: Diagnostics, args, *, title: str = "") -> int:
    items = d.sorted()
    if not getattr(args, "verbose", False):
        items = [x for x in items if x.severity != "info"]
    if getattr(args, "json", False):
        print(json.dumps([x.to_dict() for x in items], indent=2, ensure_ascii=False))
    else:
        # Aynı kök neden (kod + mesaj) birden çok dokümanda tekrarlanıyorsa ilkini göster, gerisini özetle.
        groups: dict[tuple, list] = {}
        for x in items:
            groups.setdefault((x.severity, x.code, x.message), []).append(x)
        for grp in groups.values():
            print(grp[0].format())
            if len(grp) > 1 and not getattr(args, "all_locations", False):
                others = sorted({g.doc or "?" for g in grp[1:]})
                more = f" (+{len(others) - 5})" if len(others) > 5 else ""
                print(f"        ↳ +{len(grp) - 1} başka konum: {', '.join(others[:5])}{more}")
            elif len(grp) > 1:
                for g in grp[1:]:
                    print(g.format())
        e, w = len(d.errors), len(d.warnings)
        status = "BAŞARILI" if e == 0 else "HATALI"
        print(f"\n{title}{status}: {e} hata, {w} uyarı, {len(d.items) - e - w} bilgi", file=sys.stderr)
    strict = getattr(args, "strict", False)
    return 1 if d.errors or (strict and d.warnings) else 0


def _write(obj, out: str | None) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"yazıldı: {out}", file=sys.stderr)
    else:
        print(text)


def cmd_validate(args) -> int:
    ws, d = _load(args)
    Validator(ws, d).run(compile_scenarios=not args.no_scenarios)
    print(f"{len(ws.packages)} paket, {len(ws.docs)} doküman yüklendi", file=sys.stderr)
    return _report(d, args)


def cmd_list(args) -> int:
    ws, d = _load(args)
    for doc in sorted(ws.docs.values(), key=lambda x: (x.kind, x.id)):
        if args.kind and doc.kind != args.kind:
            continue
        flag = " (abstract)" if doc.abstract else ""
        ext = f"  <- {', '.join(doc.extends)}" if doc.extends else ""
        print(f"{doc.kind:17} {doc.id}{flag}{ext}")
    return 0


def cmd_tree(args) -> int:
    ws, d = _load(args)
    children = ws.children()
    roots = [x for x in ws.docs.values() if not x.extends and (not args.kind or x.kind == args.kind)]

    def walk(i: str, depth: int, seen: set) -> None:
        doc = ws.docs[i]
        print("  " * depth + ("└─ " if depth else "") + i + (" (abstract)" if doc.abstract else ""))
        if i in seen:
            return
        for c in sorted(children.get(i, [])):
            walk(c, depth + 1, seen | {i})

    for kind in sorted({r.kind for r in roots}):
        print(f"[{kind}]")
        for r in sorted((x for x in roots if x.kind == kind), key=lambda x: x.id):
            walk(r.id, 1, set())
    return 0


def cmd_resolve(args) -> int:
    ws, d = _load(args)
    r = ws.resolve(args.id)
    if r is None:
        print(f"'{args.id}' çözülemedi", file=sys.stderr)
        return _report(d, args) or 1
    out = r.as_document()
    out["x-lineage"] = r.linear
    if args.provenance:
        out["x-provenance"] = r.provenance
    _write(out, args.output)
    return 0


def cmd_describe(args) -> int:
    ws, d = _load(args)
    print(describe(ws, args.id))
    return 0


def cmd_compile(args) -> int:
    ws, d = _load(args)
    out = Builder(ws, d).compile_scenario(args.scenario)
    if out is not None and not d.has_errors():
        # Sözleşme kontrolü: üretilen çıktı motor şemasına uymalı (araç hatası güvencesi).
        ws.schema_validate(out, "compiled-scenario.schema.json", f"{args.scenario} (compiled)")
    rc = _report(d, args, title=f"{args.scenario}: ")
    if out is not None and (rc == 0 or args.force):
        _write(out, args.output)
    return rc


TRACE_TYPES = {"event", "rule.fire", "rule.blocked", "channel.preempted", "channel.released", "action", "action.skipped",
               "action.error", "command", "command.failed"}


def cmd_run(args) -> int:
    from .runtime import World, format_record, run_script

    script = json.loads(Path(args.script).read_text(encoding="utf-8"))
    sid = args.scenario or script.get("scenario")
    if not sid:
        print("senaryo belirtilmedi (argüman veya betikte 'scenario')", file=sys.stderr)
        return 2
    ws, d = _load(args)
    compiled = Builder(ws, d).compile_scenario(sid)
    if compiled is None or d.has_errors():
        return _report(d, args, title=f"{sid}: ")
    world = World(compiled)
    log = run_script(world, script)
    trace = [r.to_dict() for r in log if r.type in TRACE_TYPES]
    if args.golden:
        _write(trace, args.golden)
    if args.json:
        print(json.dumps({"trace": trace, "final": world.snapshot()}, indent=2, ensure_ascii=False))
    else:
        for r in log:
            print(format_record(r))
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    root = Path(args.root) if args.root else Workspace.find_root(Path.cwd())
    if root is None:
        print(f"{WORKSPACE_FILE} bulunamadı", file=sys.stderr)
        return 2
    serve(root, args.port)
    return 0


def cmd_catalog(args) -> int:
    ws, d = _load(args)
    _write(build_catalog(ws), args.output)
    return 1 if d.errors else 0


def cmd_derive(args) -> int:
    ws, d = _load(args)
    p = derive(ws, d, args.base, args.new_id, name=args.name, abstract=args.abstract)
    if p:
        print(f"oluşturuldu: {ws.rel(p)}")
        return 0
    return _report(d, args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="simcfg", description="Simülasyon konfigürasyon araç zinciri")
    ap.add_argument("--root", help=f"çalışma alanı kökü ({WORKSPACE_FILE} içeren dizin)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="tüm paketleri doğrula (şema + semantik + senaryo derleme)")
    p.add_argument("--strict", action="store_true", help="uyarıları da hata say")
    p.add_argument("--json", action="store_true", help="tanıları JSON olarak yaz (editör entegrasyonu)")
    p.add_argument("-v", "--verbose", action="store_true", help="bilgi mesajlarını da göster")
    p.add_argument("--no-scenarios", action="store_true", help="senaryoları derleme")
    p.add_argument("--all-locations", action="store_true", help="tekrarlanan tanıların tüm konumlarını göster")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("list", help="dokümanları listele")
    p.add_argument("--kind")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("tree", help="kalıtım ağacı")
    p.add_argument("--kind")
    p.set_defaults(fn=cmd_tree)

    p = sub.add_parser("resolve", help="kalıtımı çözülmüş dokümanı yaz")
    p.add_argument("id")
    p.add_argument("--provenance", action="store_true", help="her değerin hangi katmandan geldiği")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("describe", help="şablonun okunabilir özeti (değer + kaynak)")
    p.add_argument("id")
    p.set_defaults(fn=cmd_describe)

    p = sub.add_parser("compile", help="senaryoyu motor için tek, düzleştirilmiş JSON'a derle")
    p.add_argument("scenario")
    p.add_argument("-o", "--output")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--force", action="store_true", help="hata olsa da çıktı yaz")
    p.set_defaults(fn=cmd_compile)

    p = sub.add_parser("run", help="derlenmiş senaryoyu referans kural motorunda bir olay betiğiyle çalıştır")
    p.add_argument("script", help="olay betiği (examples/scripts/*.json)")
    p.add_argument("--scenario", help="betikteki 'scenario' alanını geçersiz kıl")
    p.add_argument("--json", action="store_true", help="izi ve son durumu JSON olarak yaz")
    p.add_argument("--golden", help="uyumluluk karşılaştırması için izi bu dosyaya yaz")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("serve", help="editör prototipini yerel olarak başlat (127.0.0.1)")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("catalog", help="editörler için tip kataloğu üret")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_catalog)

    p = sub.add_parser("derive", help="bir dokümandan türetilmiş yeni doküman iskeleti oluştur")
    p.add_argument("base")
    p.add_argument("new_id")
    p.add_argument("--name")
    p.add_argument("--abstract", action="store_true")
    p.set_defaults(fn=cmd_derive)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
