import {test} from 'node:test'
import assert from 'node:assert/strict'
import {onboardMCP,catalog,createMCPRelay} from '../dist/mcp.js'

const opts={issuer:'https://id.example/realm',gateway:'https://gateway.example/ai',openBrowser:false}
const json=(v,status=200)=>new Response(JSON.stringify(v),{status,headers:{'Content-Type':'application/json'}})
test('onboarding passes no PAT through CLI, validates URL, polls, and does not return credentials',async()=>{
  const calls=[],id='ms_fixture',url=opts.gateway+'/auth/mcp/setup#'+id+'.secret'
  let polls=0,announced
  const result=await onboardMCP(opts,'cg_gc_FIXTURE.secret',async(target,init)=>{
    calls.push({url:target,init});assert.equal(init.redirect,'error');assert.equal(init.headers.Authorization,'Bearer cg_gc_FIXTURE.secret')
    return target.endsWith('/setup')?json({id,url}):json({complete:++polls===2})
  },undefined,async()=>{},value=>{announced=value})
  assert.equal(result,undefined);assert.equal(polls,2);assert.equal(announced,url)
  assert.ok(calls.every(c=>c.init.body===undefined))
  await assert.rejects(onboardMCP(opts,'KEY',async()=>json({id,url:'https://evil.example/#secret'}),undefined,async()=>{},()=>{}),/Invalid/)
})
test('catalog honors only known connected systems',async()=>{
  assert.deepEqual(await catalog(opts,'KEY',async()=>json({servers:[{id:'jira',connected:true},{id:'confluence',connected:false}]})),[{id:'jira',connected:true}])
  await assert.rejects(catalog(opts,'KEY',async()=>json({servers:[{id:'shell',connected:true}]})),/Invalid/)
})
test('MCP relay binds to loopback, resolves current auth, strips credentials, rejects Origin and redirect',async()=>{
  let key='cg_gc_FIXTURE.secret',seen=0,redirect=false
  const relay=await createMCPRelay(opts,async()=>key,async(url,init)=>{
    seen++;assert.equal(url,opts.gateway+'/mcp/jira');assert.equal(init.redirect,'error')
    assert.equal(init.headers.get('Authorization'),'Bearer cg_gc_FIXTURE.secret')
    assert.equal(init.headers.get('cookie'),null);assert.equal(init.headers.get('x-yai-mcp'),null)
    return redirect?new Response('',{status:307,headers:{Location:'https://evil.example'}}):json({jsonrpc:'2.0',id:1,result:{tools:[]}})
  })
  const remote=relay.remote('jira')
  try{
    assert.ok(remote.url.startsWith('http://127.0.0.1:'));assert.ok(!JSON.stringify(remote).includes(key))
    assert.equal((await fetch(remote.url)).status,403)
    assert.equal((await fetch(remote.url,{headers:{...remote.headers,Origin:'https://evil.example'}})).status,403)
    assert.equal(seen,0)
    assert.equal((await fetch(remote.url,{method:'POST',headers:{...remote.headers,Cookie:'PRIVATE'},body:'{}'})).status,200)
    assert.equal((await fetch(remote.url+'?native-metadata=ignored',{headers:remote.headers})).status,200)
    key=undefined;assert.equal((await fetch(remote.url,{headers:remote.headers})).status,401);assert.equal(seen,2)
    key='cg_gc_FIXTURE.secret';redirect=true
    const r=await fetch(remote.url,{headers:remote.headers,redirect:'follow'})
    assert.equal(r.status,502);assert.equal(r.headers.get('location'),null);assert.equal(seen,3)
  }finally{await relay.close()}
})
