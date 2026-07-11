<!-- mypowertools-materialized-source -->
# MyPowerTools tool source: {{TOOL_ID}}

This local development repository was materialized from
`artifacts/source-bundle/tools/{{TOOL_ID}}`.

Committed snapshot content:

- `original-source/`: captured original tool source;
- `current-integration/`: current MyPowerTools module, product UI, service, and related test source;
- `source-map.json`: source commit, dirty-state, and snapshot mapping;
- `README.md`: local materialization and remote migration instructions.

## Local submodule URL

The initial superproject URL is local to this machine:

```text
{{LOCAL_FILE_URL}}
```

After this repository is published, run the following commands from the
MyPowerTools superproject and commit the resulting `.gitmodules` update:

```powershell
git config -f .gitmodules submodule.tools/{{TOOL_ID}}.url <remote-url>
git submodule sync -- tools/{{TOOL_ID}}
git add .gitmodules
```

The materialization script preserves a URL that has already been changed to a
remote location and does not contact that remote.
