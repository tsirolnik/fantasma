"""Generate references from declarations without loading credentials or starting a run."""
from pathlib import Path
import argparse
import inspect
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main import build_parser
from config import Settings
from commands.base import REGISTRY
from models.finding import VulnFinding


def cell(value):
    return str(value).replace('|', r'\|').replace('\n', ' ')


def documents():
    parser = build_parser()
    cli = ['# CLI reference', '', 'Generated from [main.py](../../main.py) and [commands/](../../commands/).',
           'Regenerate with `python scripts/generate_reference.py`. Profile and runtime fallback',
           'rules are in [configuration](../configuration.md). Global flags precede the command.', '',
           '```bash', 'fantasma --verbose full --profile bugbounty --targets YOUR_AUTHORIZED_DOMAIN',
           '```', '', '## Global options', '', '```text', parser.format_help().strip(), '```', '']
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                source = Path(inspect.getfile(REGISTRY[name].handler)).relative_to(ROOT)
                cli += [f'## {name}', '', f'Source: [{source}](../../{source}).', '',
                        '```text', sub.format_help().strip(), '```', '']
    cli += ['## Offline profile tools', '', '```bash', 'fantasma profiles mechanisms',
            'fantasma profiles inspect NAME_OR_PATH', '```', '',
            'These import trusted extensions but do not start a run. See [mechanisms](../mechanisms.md).', '']
    settings = ['# Environment/settings reference', '',
                'Generated from [Settings](../../config.py) declarations without reading `.env`.',
                'Regenerate with `python scripts/generate_reference.py`.', '',
                'Variables use uppercase field names. Process environment values override `.env`',
                'in the working directory. These are declared defaults, not effective run values.',
                'See [configuration](../configuration.md) for role models and CLI/profile precedence.',
                'Not every engine consumes every setting. Default model IDs may no longer be available.', '',
                '| Variable | Python type | Declared default | Description / constraints |',
                '| --- | --- | --- | --- |']
    for name, field in Settings.model_fields.items():
        desc = field.description or 'See source declaration and consuming components.'
        if field.metadata:
            desc += ' ' + ', '.join(map(str, field.metadata))
        settings.append(f'| `{name.upper()}` | `{cell(field.annotation)}` | `{cell(repr(field.default))}` | {cell(desc)} |')
    settings += ['', 'Keep populated credentials outside Git. See [.env.example](../../.env.example).', '']
    schema = VulnFinding.model_json_schema()
    model = ['# Finding data reference', '',
             'Generated from [models/finding.py](../../models/finding.py). The complete',
             '[JSON schema](finding.schema.json) includes enums and nested definitions.',
             'Regenerate with `python scripts/generate_reference.py`.', '',
             'Acceptance and independent verification are separate; see [runtime](runtime.md).', '',
             '| Field | Schema type/reference | Required | Default | Description |',
             '| --- | --- | --- | --- | --- |']
    for name, prop in schema['properties'].items():
        typ = prop.get('type') or prop.get('$ref') or prop.get('anyOf') or prop.get('allOf')
        default = json.dumps(prop['default']) if 'default' in prop else '(not declared)'
        model.append(f"| `{name}` | {cell(typ)} | {'yes' if name in schema.get('required', []) else 'no'} | `{cell(default)}` | {cell(prop.get('description', ''))} |")
    model += ['', 'Consult the JSON schema for nested definitions and validation details.', '']
    return {'cli.md': '\n'.join(cli), 'settings.md': '\n'.join(settings),
            'findings.md': '\n'.join(model), 'finding.schema.json': json.dumps(schema, indent=2) + '\n'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    stale = []
    for name, content in documents().items():
        path = ROOT / 'docs/reference' / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if stale:
        parser.exit(1, 'Stale references: ' + ', '.join(stale) + '\n')
    print('References current' if args.check else 'References generated')


if __name__ == '__main__':
    main()
