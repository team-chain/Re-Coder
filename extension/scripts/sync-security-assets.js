const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const target=path.join(root,'media/security-audit');
fs.mkdirSync(target,{recursive:true});
fs.copyFileSync(path.resolve(root,'../policies/recoder/deploy.rego'),path.join(target,'deploy.rego'));
