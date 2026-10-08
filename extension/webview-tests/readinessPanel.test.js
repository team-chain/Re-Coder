const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {ReadinessPanel,BuildFailure,sortIssues}=require('../out/webview-test/components/ReadinessPanel.js');
const issues=[
 {code:'DOCKERIGNORE_MISSING',severity:'warning',message:'.dockerignore 없음',fix:'만드세요',auto_fix:true},
 {code:'NODE_UNUSED_BUILD_SCRIPT',severity:'error',message:'react-scripts build 가 실패',fix:'build 스크립트 삭제',auto_fix:true},
 {code:'NODE_UNDECLARED_DEPENDENCY',severity:'error',message:'lodash 미선언',fix:'npm install lodash',auto_fix:false},
];
test('errors come first and only auto-fixable issues get a fix button',()=>{
 assert.deepEqual(sortIssues(issues).map(i=>i.severity),['error','error','warning']);
 const html=renderToStaticMarkup(React.createElement(ReadinessPanel,{issues,onFix(){},fixing:null}));
 assert.match(html,/빌드·실행 실패 예상 2건/);
 assert.equal((html.match(/자동 수정<\/button>/g)||[]).length,2);
 assert.match(html,/해결: npm install lodash/);
});
test('nothing is rendered when there is nothing to say',()=>{
 assert.equal(renderToStaticMarkup(React.createElement(ReadinessPanel,{issues:[],onFix(){},fixing:null})),'');
});
test('build failure shows the real cause lines, not the Dockerfile excerpt',()=>{
 const html=renderToStaticMarkup(React.createElement(BuildFailure,{diagnosis:{code:'CRA_ENTRY_MISSING',title:'빌드 진입 파일 없음',cause:'src/index.js 없음',fix:'build 스크립트 삭제',lines:['Could not find a required file.','Name: index.js'],step:'npm run build'},raw:'47 | >>> RUN npm run build'}));
 assert.match(html,/배포 실패 — 빌드 진입 파일 없음/);
 assert.match(html,/Could not find a required file\.\nName: index\.js/);
 assert.match(html,/<details/);  // 전체 출력은 접어서
});
