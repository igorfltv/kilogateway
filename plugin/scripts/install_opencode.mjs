#!/usr/bin/env node
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import crypto from 'node:crypto'
import {fileURLToPath,pathToFileURL} from 'node:url'
import {execFileSync} from 'node:child_process'
import {isDeepStrictEqual} from 'node:util'
import jsonc from 'jsonc-parser'
import {installFile,parseConfig} from './install_kilo.mjs'

const here=path.dirname(fileURLToPath(import.meta.url)),root=path.resolve(here,'../..')
export function mergeOpenCode(content,source){
  let result=content||'{}\n'
  const before=parseConfig(result),entry=source.plugins?.[0]
  if(!entry||typeof entry.package!=='string'||!entry.options?.issuer||!entry.options?.gateway||
     source.providers?.['y.ai']?.settings?.baseURL!==entry.options.gateway+'/v1') throw Error('Expected validated OpenCode v2 source')
  if(before.plugins!==undefined&&!Array.isArray(before.plugins))throw Error('OpenCode plugins must be an array')
  const changes=[]
  const edit=(where,value,options={})=>{
    result=jsonc.applyEdits(result,jsonc.modify(result,where,value,{formattingOptions:{insertSpaces:true,tabSize:2,eol:content.includes('\r\n')?'\r\n':'\n'},...options}))
    changes.push(where.join('.'))
  }
  if(!Array.isArray(before.plugins))edit(['plugins'],[entry])
  else{
    const index=before.plugins.findIndex(x=>(typeof x==='string'?x:x?.package)===entry.package)
    if(index<0)edit(['plugins',before.plugins.length],entry,{isArrayInsertion:true})
    else if(!isDeepStrictEqual(before.plugins[index],entry))edit(['plugins',index],entry)
  }
  const existing=before.providers?.['y.ai']??{},incoming=source.providers['y.ai']
  const provider={...existing,...incoming,settings:{...existing.settings,...incoming.settings},models:{...incoming.models}}
  if(!isDeepStrictEqual(before.providers?.['y.ai'],provider))edit(['providers','y.ai'],provider)
  // A new OpenCode installation opens on Y.AI; existing user model choices are retained.
  if(before.model===undefined&&source.model!==undefined)edit(['model'],source.model)
  const after=parseConfig(result)
  for(const key of Object.keys(before)){
    if(['plugins','providers'].includes(key))continue
    if(!isDeepStrictEqual(before[key],after[key]))throw Error('Unrelated OpenCode setting changed')
  }
  for(const[id,value]of Object.entries(before.providers??{}))if(id!=='y.ai'&&!isDeepStrictEqual(value,after.providers?.[id]))throw Error('Unrelated provider changed')
  return{content:result,changes}
}
async function sha256(filename){
  const hash=crypto.createHash('sha256')
  for await(const chunk of fs.createReadStream(filename))hash.update(chunk)
  return hash.digest('hex')
}
async function main(){
  const args=process.argv.slice(2),get=name=>args.includes(name)?args[args.indexOf(name)+1]:undefined
  if(args.includes('--help')){
    console.log('Usage: yai-install-opencode --source public-v2.json [--local-plugin] [--config opencode.jsonc] [--binary /path/to/native/opencode-v2.0.26] [--bin-dir ~/.local/bin] [--apply]. Without --apply: dry-run. Never downloads a runtime or edits auth/session data.')
    return
  }
  const filename=path.resolve(get('--config')||path.join(process.env.XDG_CONFIG_HOME||path.join(os.homedir(),'.config'),'opencode/opencode.jsonc'))
  let temporary
  try{
    let sourceFile=get('--source')
    if(!sourceFile){
      fs.mkdirSync(path.join(root,'.local'),{recursive:true})
      temporary=fs.mkdtempSync(path.join(root,'.local/opencode-registration-'))
      sourceFile=path.join(temporary,'opencode.y-ai.json')
      execFileSync(path.join(root,'.venv/bin/python'),['tools/opencode_config.py','--output',sourceFile],{
        cwd:root,env:{...process.env,PYTHONPATH:path.join(root,'gateway/src')+path.delimiter+root},stdio:'inherit'})
    }
    const source=JSON.parse(fs.readFileSync(sourceFile,'utf8'))
    if(args.includes('--local-plugin'))source.plugins[0].package=pathToFileURL(path.resolve(here,'../opencode')).href
    // Validate the merge and native runtime before touching the existing config.
    mergeOpenCode(fs.existsSync(filename)?fs.readFileSync(filename,'utf8'):'{}\n',source)
    const runtimeFile=path.join(os.homedir(),'.local/share/y-ai/opencode/runtime.json')
    let binarySource=get('--binary')||process.env.YAI_OPENCODE_BINARY
    if(!binarySource&&fs.existsSync(runtimeFile))binarySource=JSON.parse(fs.readFileSync(runtimeFile,'utf8')).binary
    if(!binarySource){
      const wrapper=path.resolve(here,'../dist/opencode-cli.js')
      binarySource=(process.env.PATH||'').split(path.delimiter).map(directory=>path.join(directory,'opencode'))
        .find(filename=>fs.existsSync(filename)&&fs.realpathSync(filename)!==wrapper)
    }
    if(!binarySource&&args.includes('--apply'))throw Error('Select an existing OpenCode v2.0.26 native binary with --binary')
    let runtime
    if(binarySource){
      const binary=fs.realpathSync(path.resolve(binarySource))
      const work=fs.mkdtempSync(path.join(os.tmpdir(),'yai-opencode-version-'))
      try{
        const env={...process.env}
        for(const key of ['CONFIG','DATA','STATE','CACHE'])env['XDG_'+key+'_HOME']=path.join(work,key.toLowerCase())
        const version=execFileSync(binary,['--version'],{env,cwd:work,encoding:'utf8',timeout:30000}).trim()
        if(version!=='opencode v2.0.26')throw Error('OpenCode native runtime must be v2.0.26')
        const directory=path.join(os.homedir(),'.local/share/y-ai/opencode')
        const installed=path.join(directory,'opencode-v2.0.26')
        const launcher=path.join(path.resolve(get('--bin-dir')||path.join(os.homedir(),'.local/bin')),'opencode')
        const wrapper=path.resolve(here,'../dist/opencode-cli.js')
        if(fs.existsSync(launcher)&&(!fs.lstatSync(launcher).isSymbolicLink()||fs.realpathSync(launcher)!==wrapper))throw Error('Existing opencode executable was not replaced')
        runtime={version,binary:installed,sha256:await sha256(binary),launcher}
        if(args.includes('--apply')){
          fs.mkdirSync(directory,{recursive:true,mode:0o700})
          if(!fs.existsSync(installed)||await sha256(installed)!==runtime.sha256){
            const temp=installed+'.'+crypto.randomUUID()+'.tmp'
            try{fs.copyFileSync(binary,temp,fs.constants.COPYFILE_FICLONE);fs.chmodSync(temp,0o700);fs.renameSync(temp,installed)}
            finally{if(fs.existsSync(temp))fs.unlinkSync(temp)}
          }
          fs.writeFileSync(path.join(directory,'runtime.json'),JSON.stringify(runtime,null,2)+'\n',{mode:0o600})
          fs.mkdirSync(path.dirname(launcher),{recursive:true})
          fs.chmodSync(wrapper,0o755)
          if(!fs.existsSync(launcher))fs.symlinkSync(wrapper,launcher)
        }
      }finally{fs.rmSync(work,{recursive:true,force:true})}
    }
    const report=installFile(filename,source,args.includes('--apply'),mergeOpenCode)
    console.log(JSON.stringify({...report,...(runtime?{runtime}:{})},null,2))
    if(args.includes('--apply'))console.log('Ready: opencode auth login --provider y.ai')
  }finally{if(temporary)fs.rmSync(temporary,{recursive:true,force:true})}
}
if(process.argv[1]&&fs.existsSync(process.argv[1])&&fs.realpathSync(process.argv[1])===fileURLToPath(import.meta.url)){
  main().catch(()=>{console.error('Y.AI OpenCode v2 installation failed; inspect public inputs/runtime version. No credentials printed.');process.exitCode=1})
}
