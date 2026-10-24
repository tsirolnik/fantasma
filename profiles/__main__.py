"""Offline profile inspection: python -m profiles inspect <name-or-path>."""
import argparse
import inspect
import json

from memory.mechanisms import (
    ConditionalMechanism, HOOK_NAMES, MECHANISM_REGISTRY, Mechanism, build_mechanisms,
)
from profiles.loader import load_profile


def inspect_profile(name_or_path):
    profile = load_profile(name_or_path)
    active = []
    warnings = (["Lab applies target, workflow and role LLM settings; swarm methodology, mechanisms, scoring and coordination do not drive its sequential workers."]
                if profile.topology.mode == 'lab' else [])
    if profile.topology.mode == 'pentester':
        warnings.append("Interactive uses terminal scope and one worker; target discovery, Director reviews, batch workflow and checkpoint resume do not apply.")
    seen = set()
    for mechanism in build_mechanisms(profile.mechanisms):
        inner = mechanism.inner if isinstance(mechanism, ConditionalMechanism) else mechanism
        hooks = [hook for hook in sorted(HOOK_NAMES)
                 if getattr(type(inner), hook) is not getattr(Mechanism, hook)
                 and (not isinstance(mechanism, ConditionalMechanism) or hook in mechanism.hooks)]
        if not hooks:
            warnings.append(f"{mechanism.name}: no implemented hooks are enabled")
        if mechanism.name in seen:
            warnings.append(f"{mechanism.name}: repeated name; event effects stack but run-end dispatch uses the first instance")
        seen.add(mechanism.name)
        active.append({
            'name': mechanism.name,
            'class': f'{inspect.getfile(type(inner))}:{type(inner).__name__}',
            'active_hooks': hooks,
            'when': mechanism.when if isinstance(mechanism, ConditionalMechanism) else {},
        })
    return {
        'scope': 'Profile values with defaults; excludes full-command CLI overrides and provider environment defaults.',
        'profile': profile.summary(),
        'active_mechanisms': active,
        'warnings': warnings,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect_cmd = sub.add_parser('inspect', help='Resolve a trusted profile and construct its mechanisms; no run is started')
    inspect_cmd.add_argument('profile')
    sub.add_parser('mechanisms', help='List built-in mechanisms, constructor defaults and hook names')
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect':
            result = inspect_profile(args.profile)
        else:
            result = {name: {
                'description': inspect.getdoc(cls),
                'constructor': str(inspect.signature(cls)),
                'hooks': [hook for hook in sorted(HOOK_NAMES)
                          if getattr(cls, hook) is not getattr(Mechanism, hook)],
            } for name, cls in MECHANISM_REGISTRY.items()}
        print(json.dumps(result, indent=2))
    except (ValueError, TypeError, ImportError, OSError, AttributeError) as exc:
        parser.exit(2, f'Profile error: {exc}\n')


if __name__ == '__main__':
    main()
