import {test} from 'node:test'
import assert from 'node:assert/strict'
import {begin,config,gatewayFetch} from '../dist/device.js'

test('custom discovery/device/token hosts and gateway prefix use explicit trusted configuration',async()=>{
  const opts=config({issuer:'https://id.example/realms/company',gateway:'https://api.example/ai',openBrowser:false,
    discoveryUrl:'https://login.example/metadata',deviceUrl:'https://tokens.example/device',
    tokenUrl:'https://tokens.example/token',oidcAllowedOrigins:['https://login.example/','https://tokens.example']})
  const seen=[]
  const json=v=>new Response(JSON.stringify(v),{headers:{'content-type':'application/json'}})
  const f=async(url,init={})=>{
    seen.push([url,init])
    if(url===opts.discoveryUrl)return json({issuer:opts.issuer})
    if(url===opts.deviceUrl)return json({device_code:'DEVICE',user_code:'PUBLIC',expires_in:60,interval:1,
      verification_uri:'https://login.example/device'})
    if(url===opts.tokenUrl)return json({access_token:'ACCESS',token_type:'Bearer'})
    return json({provider:'company-ai',base_url:opts.gateway+'/v1',credential:{key:'cg_gc_fixture.secret'}})
  }
  const flow=await begin(opts,f)
  const auth=await flow.poll(async()=>{})
  assert.equal(auth.metadata.company_gateway,opts.gateway)
  assert.deepEqual(seen.map(x=>x[0]),[opts.discoveryUrl,opts.deviceUrl,opts.tokenUrl,opts.gateway+'/auth/cli/exchange'])
  assert.ok(seen.every(([,init])=>init.redirect==='error'))
  const transport=gatewayFetch(opts.gateway,auth.key,async(url,init)=>{seen.push([url,init]);return json({})})
  await transport(opts.gateway+'/v1/models')
  await assert.rejects(transport('https://api.example/v1/models'),/untrusted/)
  await assert.rejects(transport('https://api.example/ai/admin/keys'),/untrusted/)
})
test('custom OIDC endpoints cannot silently expand trusted origins',()=>{
  assert.throws(()=>config({issuer:'https://id.example',gateway:'https://api.example',
    tokenUrl:'https://evil.example/token'}),/Untrusted OIDC/)
})
