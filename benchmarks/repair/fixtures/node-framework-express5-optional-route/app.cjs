const app=require('express')();
app.get("/:file.:ext?",(req,res)=>res.send('ok'));
module.exports=app;
