const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {previewRepoName,defaultRepoName}=require('../out/webview-test/components/canvas/githubName.js');
test('새 저장소 이름 미리보기는 호스트와 같은 규칙',()=>{
 assert.deepEqual(previewRepoName('Recoder Demo'),{name:'Recoder-Demo',error:''});
 assert.deepEqual(previewRepoName('LDK511/lunch-vote'),{name:'lunch-vote',error:''});
 assert.deepEqual(previewRepoName('https://github.com/LDK511/my app.git'),{name:'my-app',error:''});
 assert.equal(previewRepoName('점심투표').name,'');assert.match(previewRepoName('점심투표').error,/영문·숫자/);
 assert.deepEqual(previewRepoName(''),{name:'',error:''});
});
test('기본 이름은 프로젝트 폴더 이름',()=>{
 assert.equal(defaultRepoName('C:\\Users\\dy981\\OneDrive\\바탕 화면\\Recoder Demo'),'Recoder-Demo');
 assert.equal(defaultRepoName('C:\\Users\\dy981\\OneDrive\\바탕 화면\\TEMP'),'TEMP');
 assert.equal(defaultRepoName('/home/u/테스트'),'');
});
