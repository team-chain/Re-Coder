'use strict';
// No AWS credentials/network required. npm run compile first.
const fs=require('node:fs'),path=require('node:path');
const {managedPermissions}=require('../out/cloud/managedPermissions');
const {managedTemplate}=require('../out/security/managedTemplate');
const [account,region,destination]=process.argv.slice(2);
if(!destination)throw Error('Usage: node scripts/export-managed-permissions.js ACCOUNT REGION OUTPUT_DIRECTORY');
const policies=managedPermissions(account,region);
fs.mkdirSync(destination,{recursive:true});
for(const [name,policy] of Object.entries(policies)){
    if(JSON.stringify(policy).length>6144)throw Error('Managed policy size limit exceeded');
    fs.writeFileSync(path.join(destination,`recoder-managed-${name}.json`),JSON.stringify(policy,null,2)+'\n');
}
for(const kind of ['security','build','container'])fs.writeFileSync(path.join(destination,`recoder-${kind}.template.json`),JSON.stringify(JSON.parse(managedTemplate(account,region,kind)),null,2)+'\n');
console.log(`AWS ${account} / ${region}: runtime + bootstrap policies written to ${destination}`);
