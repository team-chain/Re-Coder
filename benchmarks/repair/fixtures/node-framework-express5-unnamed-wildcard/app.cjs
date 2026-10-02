const app=require('express')();
app.get("/*",(req,res)=>res.send('ok'));
module.exports=app;
