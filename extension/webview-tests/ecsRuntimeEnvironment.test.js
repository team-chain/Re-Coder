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

test('HTTPS and network settings reach the approved payload without arbitrary keys',()=>{
  const network={cloudfront_domain:'d123.cloudfront.net',target_group_arn:'arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/shop/012abc',assign_public_ip:false,subnet_ids:['subnet-abc'],security_group_ids:['sg-abc']};
  assert.deepEqual(parse({environment:'',secrets:'',network:JSON.stringify(network)}),{env_vars:{},secret_refs:{},...network});
  for(const value of [{cloudfront_domain:'http://localhost'},{cloudfront_domain:'evil.example'},{target_group_arn:'invalid'},{assign_public_ip:'false'},{subnet_ids:'subnet-abc'},{security_group_ids:['0.0.0.0/0']},{skip_opa:true},null,[]]) {
    assert.throws(()=>parse({environment:'',secrets:'',network:JSON.stringify(value)}));
  }
});
