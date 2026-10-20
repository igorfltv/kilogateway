import {test} from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import {execFileSync} from 'node:child_process'
import {openBrowser} from '../dist/browser.js'
import plugin from '../dist/opencode.js'
import {createOpenCodeTransport} from '../dist/opencode-transport.js'
import {normalizeArgs} from '../dist/opencode-cli.js'
import {mergeOpenCode} from '../scripts/install_opencode.mjs'
import {parseConfig} from '../scripts/install_kilo.mjs'

const opts={issuer:'https://id.example/realms/company',gateway:'https://gateway.example/ai',openBrowser:false}
test('installer modules can be imported from a Node eval command with URI arguments',()=>{
  const uri=new URL('../scripts/install_opencode.mjs',import.meta.url).href
  execFileSync(process.execPath,['--input-type=module','-e','await import(process.argv[1])',uri],{stdio:'pipe'})
})
test('OpenCode CLI compatibility only translates the auth provider selector',()=>{
  assert.deepEqual(normalizeArgs(['auth','login','--provider','y.ai']),['auth','login','y.ai'])
  assert.deepEqual(normalizeArgs(['auth','login','--provider=y.ai','--standalone']),['auth','login','y.ai','--standalone'])
  assert.deepEqual(normalizeArgs(['auth','login','y.ai']),['auth','login','y.ai'])
  assert.deepEqual(normalizeArgs(['run','--model','y.ai/test','--standalone']),['run','--model','y.ai/test','--standalone'])
  assert.throws(()=>normalizeArgs(['auth','login','--provider']),/requires/)
  assert.throws(()=>normalizeArgs(['auth','login','--provider','a','--provider=b']),/once/)
})
test('OpenCode v2 installer preserves native plural settings, comments, other providers and default model',()=>{
  const source={plugins:[{package:'file:///plugin/dist/opencode.js',options:opts}],
    providers:{'y.ai':{name:'Y.AI',settings:{baseURL:opts.gateway+'/v1'},models:{coder:{capabilities:{tools:true}}}}},model:'y.ai/coder'}
  const original='// KEEP\n'+JSON.stringify({model:{providerID:'other',model:'default'},
    plugins:['existing-plugin'],providers:{other:{settings:{apiKey:'KEEP_FIXTURE'}},'y.ai':{models:{old:{}}}},
    agents:{build:{permissions:[{action:'edit',effect:'ask'}]}}})
  const merged=mergeOpenCode(original,source),after=parseConfig(merged.content)
  assert.ok(merged.content.includes('// KEEP'))
  assert.deepEqual(after.model,{providerID:'other',model:'default'})
  assert.equal(after.providers.other.settings.apiKey,'KEEP_FIXTURE')
  assert.deepEqual(after.providers['y.ai'].models,source.providers['y.ai'].models)
  assert.deepEqual(after.agents,parseConfig(original).agents)
  assert.equal(mergeOpenCode(merged.content,source).content,merged.content)
  assert.equal(parseConfig(mergeOpenCode('{}',source).content).model,'y.ai/coder')
  assert.throws(()=>mergeOpenCode('{"plugins":{}}',source),/array/)
  assert.throws(()=>mergeOpenCode('{}',{...source,providers:{}}),/validated/)
})
test('OpenCode v2 registers native OAuth, stores only gateway credential, and identifies the client',async()=>{
  const registrations=[],hooks=[],calls=[]
  let method
  const provider={id:'y.ai',settings:{baseURL:'https://evil.example',apiKey:'WRONG'}}
  const ctx={options:opts,provider:{transform:async fn=>{fn({get:()=>({provider}),update:(_,edit)=>edit(provider)});return{dispose:async()=>registrations.push('provider')}}},
    integration:{transform:async fn=>{fn({get:()=>({}),update:(_,edit)=>edit({}),method:{list:()=>[],remove:()=>{},update:entry=>{method=entry}}});return{dispose:async()=>registrations.push('auth')}},
      connection:{active:async()=>({}),resolve:async()=>undefined}},session:{hook:async(name,fn,scope)=>{hooks.push({name,fn,scope});return{dispose:async()=>registrations.push(name)}}}}
  const previous=globalThis.fetch
  const expiry=Date.now()+3600000
  globalThis.fetch=async(url,init={})=>{
    calls.push({url,init})
    const json=data=>new Response(JSON.stringify(data),{headers:{'content-type':'application/json'}})
    if(url.endsWith('openid-configuration'))return json({issuer:opts.issuer,device_authorization_endpoint:opts.issuer+'/device',token_endpoint:opts.issuer+'/token'})
    if(url.endsWith('/device'))return json({device_code:'DEVICE_SECRET',user_code:'PUBLIC',verification_uri:opts.issuer+'/verify',expires_in:60,interval:1})
    if(url.endsWith('/token'))return json({access_token:'IDP_SECRET',refresh_token:'REFRESH_SECRET',token_type:'Bearer'})
    return json({provider:'company-ai',base_url:opts.gateway+'/v1',credential:{key:'cg_gc_fixture.secret',expires_at:new Date(expiry).toISOString()}})
  }
  let cleanup
  try{
    cleanup=await plugin.setup(ctx)
    assert.equal(method.integrationID,'y.ai');assert.equal(method.method.type,'oauth')
    assert.equal(provider.settings.baseURL,opts.gateway+'/v1');assert.equal(provider.settings.apiKey,'yai-plugin-managed')
    assert.ok(hooks.every(h=>h.scope.providerID==='y.ai'))
    const flow=await method.authorize({}),value=await flow.callback
    assert.equal(flow.mode,'auto');assert.equal(value.type,'oauth');assert.equal(value.access,'cg_gc_fixture.secret')
    assert.equal(value.refresh,'');assert.equal(value.expires,expiry)
    assert.deepEqual(value.metadata,{company_gateway:opts.gateway,company_issuer:opts.issuer})
    assert.ok(!JSON.stringify(value).includes('IDP_SECRET'));assert.ok(!JSON.stringify(value).includes('REFRESH_SECRET'))
    const exchange=calls.find(x=>x.url.endsWith('/exchange'))
    assert.equal(JSON.parse(exchange.init.body).client,'opencode-cli')
    assert.equal(exchange.init.headers.Authorization,'Bearer IDP_SECRET')
    await assert.rejects(method.refresh(value),/opencode auth login --provider y.ai/)
    const requestHook=hooks.find(h=>h.name==='http.request')
    assert.throws(()=>requestHook.fn({request:new Request('https://evil.example/v1/models')}),/untrusted/)
  }finally{globalThis.fetch=previous;await cleanup?.()}
  assert.equal(registrations.length,5)
})
test('local relay requires capability, strips native credentials and checks targets before loading a key',async()=>{
  let reads=0,calls=0
  const relay=await createOpenCodeTransport(opts.gateway,async()=>{reads++;return'cg_gc_REAL.secret'},async(url,init)=>{
    calls++;assert.equal(url,opts.gateway+'/v1/models');assert.equal(init.headers.get('authorization'),'Bearer cg_gc_REAL.secret')
    assert.equal(init.headers.get('cookie'),null);assert.equal(init.redirect,'error')
    return new Response('{}',{headers:{'content-type':'application/json'}})
  })
  try{
    assert.throws(()=>relay.request(new Request('https://evil.example/v1/models')),/untrusted/)
    assert.throws(()=>relay.request(new Request(opts.gateway+'/admin/keys')),/untrusted/)
    const local=relay.request(new Request(opts.gateway+'/v1/models',{headers:{Authorization:'NATIVE_SECRET',Cookie:'PRIVATE'}}))
    assert.equal(local.headers.get('authorization'),null);assert.equal(local.headers.get('cookie'),null)
    assert.equal((await fetch(local.url)).status,401);assert.equal(reads,0)
    assert.equal((await fetch(local)).status,200);assert.equal(reads,1);assert.equal(calls,1)
  }finally{await relay.close()}
})
test('redirects cannot escape through native redirect following and secrets are not reflected',async()=>{
  let calls=0
  const relay=await createOpenCodeTransport(opts.gateway,async()=>'cg_gc_REAL.secret',async()=>{
    calls++;return new Response('',{status:307,headers:{Location:'https://evil.example/capture',Authorization:'cg_gc_REAL.secret'}})
  })
  try{
    const response=await fetch(relay.request(new Request(opts.gateway+'/v1/models')),{redirect:'follow'})
    assert.equal(response.status,502);assert.equal(response.headers.get('location'),null);assert.equal(response.headers.get('authorization'),null)
    assert.ok(!(await response.text()).includes('cg_gc_REAL.secret'));assert.equal(calls,1)
  }finally{await relay.close()}
})
test('credential failures stay inside the relay and disposal closes it',async()=>{
  let calls=0
  const relay=await createOpenCodeTransport(opts.gateway,async()=>{throw Error('SECRET_BINDING_ERROR')},async()=>{calls++;return new Response('{}')})
  try{
    const response=await fetch(relay.request(new Request(opts.gateway+'/v1/models')))
    assert.equal(response.status,502);assert.ok(!(await response.text()).includes('SECRET_BINDING_ERROR'));assert.equal(calls,0)
  }finally{await relay.close()}
  assert.throws(()=>relay.request(new Request(opts.gateway+'/v1/models')),/disposed/)
})

test('provider transforms tolerate native config sources being registered later',async()=>{
  let transform
  const registration=()=>({dispose:async()=>{}})
  const ctx={options:opts,provider:{transform:async fn=>{transform=fn;fn({get:()=>undefined});return registration()}},
    integration:{transform:async fn=>{fn({get:()=>undefined,method:{list:()=>[],update:()=>{},remove:()=>{}}});return registration()},
      connection:{active:async()=>undefined}},session:{hook:async()=>registration()}}
  const cleanup=await plugin.setup(ctx)
  try{
    const provider={id:'y.ai',settings:{baseURL:'https://wrong.example'}}
    transform({get:()=>({provider}),update:(_,edit)=>edit(provider)})
    assert.equal(provider.settings.baseURL,opts.gateway+'/v1');assert.equal(provider.integrationID,'y.ai')
  }finally{await cleanup()}
})
test('automatic browser opening passes the complete verification URL to the system opener',async()=>{
  if(process.platform==='win32')return
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'yai-browser-'))
  const command=path.join(directory,process.platform==='darwin'?'open':'xdg-open')
  const output=path.join(directory,'opened.txt')
  fs.writeFileSync(command,'#!/bin/sh\nprintf "%s" "$1" > "$YAI_BROWSER_TEST_LOG"\n',{mode:0o700})
  const previousPath=process.env.PATH,previousOutput=process.env.YAI_BROWSER_TEST_LOG
  process.env.PATH=directory+path.delimiter+previousPath;process.env.YAI_BROWSER_TEST_LOG=output
  try{
    const url='http://localhost:28080/realms/company/device?user_code=TEST-CODE'
    openBrowser(url)
    for(let i=0;i<100&&!fs.existsSync(output);i++)await new Promise(resolve=>setTimeout(resolve,20))
    assert.equal(fs.readFileSync(output,'utf8'),url)
  }finally{
    process.env.PATH=previousPath
    if(previousOutput===undefined)delete process.env.YAI_BROWSER_TEST_LOG;else process.env.YAI_BROWSER_TEST_LOG=previousOutput
    fs.rmSync(directory,{recursive:true,force:true})
  }
})
