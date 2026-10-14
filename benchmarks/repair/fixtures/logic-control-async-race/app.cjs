exports.total=async values=>{let s=0;values.forEach(async x=>{s+=await Promise.resolve(x)});return s};
