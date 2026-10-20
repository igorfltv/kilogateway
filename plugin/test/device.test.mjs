import { test } from "node:test"
import assert from "node:assert/strict"
import { begin, gatewayFetch, trusted } from "../dist/device.js"
import plugin from "../dist/index.js"

const opts = {issuer:"https://id.example/realms/company",gateway:"https://gateway.example",clientId:"kilo-cli"}
const json = (v,status=200) => new Response(JSON.stringify(v),{status,headers:{"content-type":"application/json"}})
function mock(tokens) {
  const calls=[]
  const fetcher=async(url,init={})=>{
    calls.push({url,init})
    if(url.endsWith("openid-configuration")) return json({issuer:opts.issuer,device_authorization_endpoint:opts.issuer+"/device",token_endpoint:opts.issuer+"/token"})
    if(url.endsWith("/device")) return json({device_code:"DEVICE_SECRET",user_code:"PUBLIC",verification_uri:"https://id.example/verify",expires_in:60,interval:1})
    if(url.endsWith("/token")) return json(tokens.shift(), tokens.length ? 400 : 200)
    return json({provider:"company-ai",base_url:opts.gateway+"/v1",credential:{key:"cg_gc_id.secret"}})
  }
  return {fetcher,calls}
}

test("pending, slow_down and only gateway credential returned",async()=>{
 const m=mock([{error:"authorization_pending"},{error:"slow_down"},{access_token:"ACCESS_SECRET",id_token:"ID_SECRET",refresh_token:"REFRESH_SECRET",token_type:"Bearer"}])
 const flow=await begin(opts,m.fetcher), waits=[]
 const result=await flow.poll(async(ms)=>{waits.push(ms)})
 assert.deepEqual(waits,[1000,1000,6000]);assert.deepEqual(result,{type:"success",key:"cg_gc_id.secret",
  metadata:{company_gateway:opts.gateway,company_issuer:opts.issuer}})
 assert.ok(!JSON.stringify(result).includes("ACCESS_SECRET"))
 const ex=m.calls.find(x=>x.url.endsWith("exchange"))
 assert.equal(ex.init.headers.Authorization,"Bearer ACCESS_SECRET")
 assert.ok(!JSON.stringify(ex).includes("ID_SECRET"));assert.ok(m.calls.every(x=>x.init.redirect==="error"))
})
for(const [error,message] of [["access_denied","access denied"],["expired_token","expired"]])test(error,async()=>{
 const m=mock([{error}]),flow=await begin(opts,m.fetcher)
 await assert.rejects(flow.poll(async()=>{}),new RegExp(message))
})
test("cancellation",async()=>{
 const controller=new AbortController(),m=mock([{error:"authorization_pending"}]);const flow=await begin(opts,m.fetcher,controller.signal)
 controller.abort();await assert.rejects(flow.poll(),/cancelled/)
})
test("unavailable network terminates with a safe message",async()=>{
 const m=mock([]),f=async(url,init)=>url.endsWith("/token")?Promise.reject(new Error("SECRET_TOKEN")):m.fetcher(url,init)
 const flow=await begin(opts,f)
 await assert.rejects(flow.poll(async()=>{}),/^Error: Company SSO network unavailable$/)
})
test("transport rejects foreign origins, alternate endpoints and redirects",async()=>{
 let seen
 const f=gatewayFetch(opts.gateway,"GATEWAY_SECRET",async(url,init)=>{seen=init;return json({})})
 await assert.rejects(f("https://evil.example/v1/chat/completions"),/untrusted/)
 await assert.rejects(f(opts.gateway+"/v1/responses"),/untrusted/)
 await f(opts.gateway+"/v1/chat/completions",{headers:{Cookie:"PRIVATE",Authorization:"WRONG","Content-Type":"application/json"}})
 assert.equal(seen.headers.get("authorization"),"Bearer GATEWAY_SECRET");assert.equal(seen.headers.get("cookie"),null);assert.equal(seen.redirect,"error")
})
test("HTTP restricted to explicit loopback dev",()=>{
 assert.throws(()=>trusted("http://localhost:1234"));assert.throws(()=>trusted("http://evil.example",true));assert.ok(trusted("http://localhost:1234",true))
})
test("overall deadline is bounded even while authorization remains pending",async()=>{
 const original=Date.now;let now=original();Date.now=()=>now
 try {
  const m=mock(Array.from({length:100},()=>({error:"authorization_pending"}))),flow=await begin(opts,m.fetcher)
  await assert.rejects(flow.poll(async(ms)=>{now+=ms}),/expired/)
  assert.ok(m.calls.filter(x=>x.url.endsWith("/token")).length<61)
 } finally {Date.now=original}
})
test("stored credential cannot follow a changed project gateway",async()=>{
 const hooks=await plugin({},opts)
 assert.equal(hooks.auth.provider,"y.ai")
 const conf={}
 await hooks.config(conf)
 assert.deepEqual(Object.keys(conf.provider),["y.ai"])
 assert.equal(conf.provider["y.ai"].name,"Y.AI")
 await assert.rejects(hooks.auth.loader(async()=>({type:"api",key:"GATEWAY_SECRET",metadata:{
  company_gateway:"https://other.example",company_issuer:opts.issuer}})),/bound to another/)
 const loaded=await hooks.auth.loader(async()=>({type:"api",key:"GATEWAY_SECRET",metadata:{
  company_gateway:opts.gateway,company_issuer:opts.issuer}}))
 assert.equal(loaded.baseURL,opts.gateway+"/v1")
})
