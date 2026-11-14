"""Offline runtime-only VSIX and companion source archive."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import zipfile

def runtime_files(source):
    tree=ast.parse((source/'harness/governance.py').read_text())
    policies=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='POLICY_FILES' for t in n.targets))
    files={source/'harness'/name for name in policies}|{source/'THIRD_PARTY_NOTICES.md'}
    pending=[p for p in files if p.suffix=='.py'];seen=set()
    while pending:
        p=pending.pop()
        if p in seen:continue
        seen.add(p)
        for node in ast.walk(ast.parse(p.read_text())):
            names=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module] if isinstance(node,ast.ImportFrom) and node.module else []
            for name in names:
                dep=source/'harness'/(name.replace('.','/')+'.py')
                if dep.is_file() and dep not in files:files.add(dep);pending.append(dep)
    files.update(source/name for name in ('package.json','extension.js','model_selection.js','native_provider.js','native_memory.js','diagram_view.js','usage_view.js','host_tokens.js','README.txt','runtime-integrity.json','bin/telemetry','bin/telemetry-build.json','harness/canonical-kernel.json','harness/README.txt'))
    files.update(p for directory in ('media','vendor') for p in (source/directory).rglob('*') if p.is_file())
    assert not any(p.name.startswith(('test_','experiment','validate_','audit_')) for p in files)
    return files


def integrity_hashes(source):
    excluded = {"package.json", "runtime-integrity.json"}
    return {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(runtime_files(source))
            if p.relative_to(source).as_posix() not in excluded}


def verify_integrity(source):
    manifest = json.loads((source / "runtime-integrity.json").read_text())
    if manifest.get("format") != 2 or manifest["sha256"] != integrity_hashes(source):
        raise ValueError("Runtime integrity coverage or hashes differ; regenerate explicitly after reviewing changes")


def package(source, output):
    output.mkdir(parents=True,exist_ok=True)
    pkg=json.loads((source/'package.json').read_text());version=pkg['version']
    files=runtime_files(source)
    verify_integrity(source)
    kernel=hashlib.sha256((source/'harness/stp_canonical/kernel.py').read_bytes()).hexdigest()
    assert kernel=='9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac'
    manifest=f'''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
<Metadata><Identity Language="en-US" Id="{pkg['name']}" Version="{version}" Publisher="{pkg['publisher']}"/>
<DisplayName>{pkg['displayName']}</DisplayName><Description xml:space="preserve">Sutra Auto native chat, governed local Python edits/tests and unchanged STP governance.</Description>
<Tags>local,ollama,context</Tags><Categories>Other</Categories><GalleryFlags/>
<Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="{pkg['engines']['vscode']}"/><Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/><Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/><Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace"/></Properties></Metadata>
<Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation><Dependencies/>
<Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets></PackageManifest>'''
    types='''<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/><Default Extension="cjs" ContentType="application/javascript"/><Default Extension="html" ContentType="text/html"/><Default Extension="py" ContentType="text/plain"/><Default Extension="txt" ContentType="text/plain"/><Default Extension="ts" ContentType="text/plain"/><Default Extension="svg" ContentType="image/svg+xml"/><Default Extension="h" ContentType="text/plain"/><Default Extension="vsixmanifest" ContentType="text/xml"/><Default Extension="m" ContentType="text/plain"/></Types>'''
    vsix=output/f"Sutra-{version}.vsix"
    with zipfile.ZipFile(vsix,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('extension.vsixmanifest',manifest);z.writestr('[Content_Types].xml',types)
        for p in sorted(files):z.write(p,'extension/'+p.relative_to(source).as_posix())
    with zipfile.ZipFile(vsix) as z:assert z.testzip() is None
    source_files=set(files)|{source/name for name in ('package.py','README.md','CHANGELOG.md','THIRD_PARTY_NOTICES.md','.gitignore')}
    source_files.update(p for p in (source/'docs').rglob('*') if p.is_file())
    source_files.update((source/'harness').glob('*.py'));source_files.update((source/'test').glob('*'))
    source_files.update(p for p in (source/'scripts').rglob('*') if p.is_file())
    source_files.update(p for p in (source/'.github').rglob('*') if p.is_file())
    source_files.update(p for p in (source/'examples').rglob('*') if p.is_file())
    with zipfile.ZipFile(output/f'Sutra-{version}-source.zip','w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(source_files):
            if p.is_file():z.write(p,p.relative_to(source))
    result={'version':version,'vsix':vsix.name,'sha256':hashlib.sha256(vsix.read_bytes()).hexdigest(),'bytes':vsix.stat().st_size,'runtime_files':len(files),'kernel_sha256':kernel,'runtime_tests_included':False}
    (output/'Package.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    package(args.source.resolve(),args.output.resolve())
