import { test } from "node:test"
import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { installFile, mergeCompany, parseConfig } from "../scripts/install_kilo.mjs"

const source = {plugin:[["file:///example/plugin.js",{issuer:"https://sso.example/realms/company",gateway:"https://gateway.example"}]],
  provider:{"y.ai":{name:"Y.AI",npm:"@ai-sdk/openai-compatible@2.0.48",options:{baseURL:"https://gateway.example/v1"},models:{coder:{tool_call:true}}}}}
const original = `// KEEP_HEADER
{
  "model": "existing/default",
  "provider": {
    // KEEP_PROVIDER
    "existing": {"options":{"apiKey":"FAKE_SECRET"}}
  },
  "plugin": [
    // KEEP_PLUGIN
    "existing-plugin",
  ],
  "agent": {"build":{"permission":"ask"}},
}
`
test("merge preserves comments, other providers, default model and permissions",()=>{
  const merged=mergeCompany(original,source)
  for(const marker of ["KEEP_HEADER","KEEP_PROVIDER","KEEP_PLUGIN","FAKE_SECRET"]) assert.ok(merged.content.includes(marker))
  const before=parseConfig(original), after=parseConfig(merged.content)
  assert.deepEqual(after.provider.existing,before.provider.existing)
  assert.deepEqual(after.agent,before.agent);assert.equal(after.model,before.model)
  assert.deepEqual(after.plugin,["existing-plugin",source.plugin[0]])
  assert.equal(mergeCompany(merged.content,source).content,merged.content)
})
test("provider allow/deny lists enable only y.ai",()=>{
  const input=JSON.stringify({enabled_providers:["existing"],disabled_providers:["other","y.ai"]})
  const c=parseConfig(mergeCompany(input,source).content)
  assert.deepEqual(c.enabled_providers,["existing","y.ai"]);assert.deepEqual(c.disabled_providers,["other"])
})
test("managed model catalog replaces stale company models while preserving other providers",()=>{
  const input=JSON.stringify({provider:{"y.ai":{models:{stale:{name:"Old"}}},other:{models:{keep:{}}}}})
  const c=parseConfig(mergeCompany(input,source).content)
  assert.deepEqual(c.provider["y.ai"].models,source.provider["y.ai"].models)
  assert.deepEqual(c.provider.other.models,{keep:{}})
})
test("legacy provider migrates with model references and allow lists, preserving unrelated settings",()=>{
  const before={model:"company-ai/coder",small_model:"company-ai/fast",
    enabled_providers:["existing","company-ai","y.ai"],disabled_providers:["other","company-ai","y.ai"],
    provider:{"company-ai":{options:{timeout:1000},models:{stale:{}}},existing:{models:{keep:{}}}},
    agent:{build:{model:"company-ai/coder",permission:"ask"},review:{model:"existing/review"}}}
  const merged=mergeCompany('// KEEP\n'+JSON.stringify(before),source)
  const after=parseConfig(merged.content)
  assert.ok(merged.content.includes('// KEEP'))
  assert.equal(after.provider['company-ai'],undefined)
  assert.equal(after.provider['y.ai'].options.timeout,1000)
  assert.deepEqual(after.provider['y.ai'].models,source.provider['y.ai'].models)
  assert.equal(after.model,'y.ai/coder');assert.equal(after.small_model,'y.ai/fast')
  assert.deepEqual(after.agent,{build:{model:'y.ai/coder',permission:'ask'},review:before.agent.review})
  assert.deepEqual(after.enabled_providers,['existing','y.ai']);assert.deepEqual(after.disabled_providers,['other'])
  assert.deepEqual(after.provider.existing,before.provider.existing)
  assert.equal(mergeCompany(merged.content,source).content,merged.content)
})
test("invalid JSONC or plugin shape is rejected",()=>{
  assert.throws(()=>mergeCompany('{"plugin":',source),/Invalid/)
  assert.throws(()=>mergeCompany('{"plugin":{}}',source),/not an array/)
})
test("installer is dry-run by default, backs up exact bytes, and is idempotent",()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),"company-kilo-install-")),file=path.join(dir,"kilo.jsonc")
  try {
    fs.writeFileSync(file,original)
    assert.equal(installFile(file,source).status,"dry-run");assert.equal(fs.readFileSync(file,"utf8"),original)
    const report=installFile(file,source,true)
    assert.equal(report.status,"installed");assert.equal(fs.readFileSync(report.backup,"utf8"),original)
    assert.equal(fs.statSync(report.backup).mode&0o777,0o600)
    assert.equal(installFile(file,source,true).status,"unchanged")
  } finally {fs.rmSync(dir,{recursive:true,force:true})}
})
