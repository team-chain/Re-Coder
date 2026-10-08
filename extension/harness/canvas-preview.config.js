const path=require('node:path');
module.exports={mode:'development',entry:path.join(__dirname,'canvas-preview.js'),output:{path:path.resolve(__dirname,'../../.canvas-qa/preview'),filename:'preview.js'},resolve:{extensions:['.js']},devtool:false,performance:{hints:false}};
