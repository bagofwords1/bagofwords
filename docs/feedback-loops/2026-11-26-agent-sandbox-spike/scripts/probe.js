const net=require('net'),dns=require('dns');
const T={"other-sandbox:8080":["10.213.77.10",8080],"caddy:8443":["10.213.77.2",8443],"egress:3128":["10.213.77.3",3128],"host-gw:22":["10.213.77.1",22],"host-ip:22":["SANDBOX_HOST",22],"host-ip:8443":["SANDBOX_HOST",8443],"devbox:22":["BOW_HOST",22],"metadata:80":["169.254.169.254",80],"internet 1.1.1.1:443":["1.1.1.1",443]};
const tcp=(h,p)=>new Promise(r=>{const s=net.connect({host:h,port:p,timeout:3000});s.on('connect',()=>{s.destroy();r('OPEN')});s.on('timeout',()=>{s.destroy();r('TimeoutError')});s.on('error',e=>r(e.code))});
(async()=>{const o={};for(const k in T)o[k]=await tcp(...T[k]);dns.lookup('pypi.org',(e,a)=>console.log(JSON.stringify(o),'dns:',e?e.code:a))})();
