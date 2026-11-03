#!/bin/sh
set -eu
case "$1" in
  .doc|.docx|.ppt|.pptx) suffix="$1"; filter=pdf; output=pdf ;;
  fixture-doc) suffix=.docx; filter="doc:MS Word 97"; output=doc ;;
  *) exit 4 ;;
esac
umask 077
mkdir -p "$HOME/profile/user" /tmp/out
cat > "$HOME/profile/user/registrymodifications.xcu" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
    <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
  </item>
</oor:items>
XML
cat > "/tmp/source$suffix"
soffice -env:UserInstallation=file:///tmp/home/profile --headless --nologo --norestore --nodefault \
    --convert-to "$filter" --outdir /tmp/out "/tmp/source$suffix" >/dev/null 2>&1
test -s "/tmp/out/source.$output"
cat "/tmp/out/source.$output"
