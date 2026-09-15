#!/usr/bin/env python3
"""StoreAI deploy resolver / config helper.

Reads deploy/config/registry.yaml + a storeai.config.json and provides:
  order        Print enabled modules (+ auto-resolved hard deps) in deploy order
  tf-targets   Print terraform -target args for the resolved terraform modules
  get KEY      Print a global value (dot path, e.g. global.region)
  validate     Validate enabled modules' required prerequisites are present

Auto-enables hard dependencies (with a warning to stderr) and topologically
sorts. Used by deploy/storeai. Requires PyYAML.

--exact turns off dependency expansion, for teardown: `down --module X` removes
exactly X. Expanding to dependencies is correct for `up` and destructive for
`down`. See _resolve.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.path.dirname(HERE), "config")
REGISTRY = os.path.join(CONFIG_DIR, "registry.yaml")


def _load_yaml(path):
    try:
        import yaml
    except ImportError:
        sys.exit("ERROR: PyYAML required (pip install pyyaml)")
    with open(path) as f:
        return yaml.safe_load(f)


def _load_json(path):
    with open(path) as f:
        return json.load(f)


def _dig(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _enabled_modules(config):
    return [m for m, v in config.get("modules", {}).items() if v.get("enabled")]


def _dependents(mods):
    """Reverse adjacency: module -> [modules that declare it in depends_on]."""
    rdeps = {}
    for m, meta in mods.items():
        for dep in (meta or {}).get("depends_on", []) or []:
            rdeps.setdefault(dep, []).append(m)
    return rdeps


def _toposort(mods, closure):
    """Deploy order within closure (Kahn), ignoring deps outside it."""
    indeg = {m: 0 for m in closure}
    for m in closure:
        for dep in mods[m].get("depends_on", []) or []:
            if dep in closure:
                indeg[m] += 1
    order = []
    ready = sorted([m for m in closure if indeg[m] == 0])
    while ready:
        m = ready.pop(0)
        order.append(m)
        for other in closure:
            if m in (mods[other].get("depends_on", []) or []):
                indeg[other] -= 1
                if indeg[other] == 0:
                    ready.append(other)
                    ready.sort()
    if len(order) != len(closure):
        sys.exit(f"ERROR: dependency cycle detected among {closure - set(order)}")
    return order


def _resolve(config, registry, requested=None, exact=False):
    """Return (ordered_modules, warnings).

    Default (deploy): adds hard dependencies transitively, because a module
    cannot come up before the things it needs.

    exact=True (teardown): resolves to exactly the requested modules and expands
    nothing. Dependency expansion is right for `up` and destructive for `down`.
    Without this, `down --module orchestrator` resolved the orchestrator's
    dependency closure (eks, network, ecr, data-plane, mcp-tools,
    litellm-gateway) and handed every one of their tf_targets to
    `terraform destroy`, tearing out the VPC, the cluster and the data plane in
    order to remove one Deployment. Dependents are reported as warnings rather
    than destroyed, which is the contract README.md and docs/getting-started.md
    already state: remove a module and nothing that depends on it.
    """
    mods = registry["modules"]
    warnings = []
    if requested:
        wanted = set(requested)
    else:
        wanted = set(_enabled_modules(config))

    for m in wanted:
        if m not in mods:
            sys.exit(f"ERROR: unknown module '{m}' (not in registry.yaml)")

    if exact:
        closure = set(wanted)
        # Anything still deployed on top of these loses what it was built on. Say so
        # instead of removing it: a destructive command should touch only what it was
        # asked to touch.
        rdeps = _dependents(mods)
        seen, stack = set(), list(wanted)
        while stack:
            m = stack.pop()
            for other in rdeps.get(m, []):
                if other in wanted or other in seen:
                    continue
                seen.add(other)
                warnings.append(
                    f"'{other}' depends on '{m}' and is not in this teardown; "
                    f"it will be left without it"
                )
                stack.append(other)
        return _toposort(mods, closure), warnings

    # transitive hard-dep closure
    closure = set()
    stack = list(wanted)
    while stack:
        m = stack.pop()
        if m in closure:
            continue
        if m not in mods:
            sys.exit(f"ERROR: unknown module '{m}' (not in registry.yaml)")
        closure.add(m)
        for dep in mods[m].get("depends_on", []) or []:
            if dep not in wanted and dep not in closure:
                warnings.append(f"auto-enabling dependency '{dep}' (required by '{m}')")
            stack.append(dep)

    return _toposort(mods, closure), warnings


def _prereq_value(config, name):
    p = config.get("prerequisites", {})
    mapping = {
        "capacity_block_reservation_id": _dig(p, "capacityBlock.reservationId"),
        "heygen_api_key": _dig(p, "avatar.apiKey"),
        "llm_model_s3_path": _dig(p, "models.llmS3Path"),
        "vton_model_s3_path": _dig(p, "models.vtonS3Path"),
        "custom_domain": _dig(p, "dns.customDomain"),
        "hosted_zone_id": _dig(p, "dns.hostedZoneId"),
        "admin_email": _dig(p, "auth.adminEmail"),
    }
    return mapping.get(name)


def cmd_order(config, registry, args):
    order, warnings = _resolve(config, registry, args.module or None, args.exact)
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    print(" ".join(order))


def cmd_tf_targets(config, registry, args):
    order, _ = _resolve(config, registry, args.module or None, args.exact)
    targets = []
    for m in order:
        for t in registry["modules"][m].get("tf_targets", []) or []:
            targets.append(t)
    print(" ".join(f"-target={t}" for t in targets))


def cmd_get(config, registry, args):
    val = _dig(config, args.key)
    print("" if val is None else val)


def cmd_validate(config, registry, args):
    order, warnings = _resolve(config, registry, args.module or None)
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    errors = []
    for m in order:
        meta = registry["modules"][m]
        if meta.get("status") == "blocked":
            errors.append(f"module '{m}' is blocked ({meta.get('note','')})")
        for req in meta.get("prereqs_required", []) or []:
            if not _prereq_value(config, req):
                errors.append(f"module '{m}' requires prerequisite '{req}' (empty in config)")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print("OK: prerequisites satisfied for: " + " ".join(order))


def main():
    ap = argparse.ArgumentParser(description="StoreAI deploy resolver")
    ap.add_argument("--config", required=True)
    ap.add_argument("--module", action="append", help="restrict to module(s) + deps")
    ap.add_argument(
        "--exact",
        action="store_true",
        help="resolve to exactly the named modules, no dependency expansion (teardown)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("order", "tf-targets", "validate"):
        sub.add_parser(name)
    g = sub.add_parser("get")
    g.add_argument("key")

    args = ap.parse_args()
    config = _load_json(args.config)
    registry = _load_yaml(REGISTRY)
    {
        "order": cmd_order,
        "tf-targets": cmd_tf_targets,
        "get": cmd_get,
        "validate": cmd_validate,
    }[args.cmd](config, registry, args)


if __name__ == "__main__":
    main()
