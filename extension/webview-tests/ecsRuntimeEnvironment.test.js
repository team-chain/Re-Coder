const test = require('node:test');
const assert = require('node:assert/strict');
const {parseRuntimeEnvironment:parse} = require('../out/webview-test/components/EcsRuntimeEnvironment.js');
const arn = 'arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:shop-db-ABCDEF';
test('runtime settings preserve strings and only accept secret references',()=>{
  assert.deepEqual(parse({environment:'{"NODE_ENV":"production","TIMEOUT":"0"}',secrets:JSON.stringify({DATABASE_URL:arn})}),{env_vars:{NODE_ENV:'production',TIMEOUT:'0'},secret_refs:{DATABASE_URL:arn}});
  for(const draft of [
    {environment:'[]',secrets:''}, {environment:'{"A":1}',secrets:''},
    {environment:'',secrets:'{"DATABASE_URL":"postgres://password@db"}'},
    {environment:'{"DATABASE_URL":"plain"}',secrets:JSON.stringify({DATABASE_URL:arn})},
    {environment:'',secrets:JSON.stringify({PORT:arn})},
    {environment:'',secrets:JSON.stringify({ENVIRONMENT:arn})},
  ]) assert.throws(()=>parse(draft));
});
