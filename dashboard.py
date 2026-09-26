#!/usr/bin/env python3
# relay dashboard: single self-contained html page, no frameworks or assets.

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>relay dashboard</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
max-width:960px;margin:32px auto;padding:0 20px;color:#1f2328;background:#fff}
h1{font-size:24px;margin:0 0 4px}
.sub{color:#57606a;font-size:13px;margin-bottom:24px}
h2{font-size:16px;margin:28px 0 8px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid #d0d7de;padding:8px 10px;text-align:left}
th{background:#f6f8fa;font-weight:600}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:baseline}
.ok{background:#1a7f37}.bad{background:#d1242f}.warn{background:#9a6700}
.num{text-align:right;font-variant-numeric:tabular-nums}
.note{color:#57606a;font-size:12px;margin-top:16px}
</style>
</head>
<body>
<h1>relay</h1>
<div class="sub" id="meta">loading...</div>
<h2>upstreams</h2>
<table>
<thead><tr><th></th><th>name</th><th>breaker</th><th class="num">avg ms</th>
<th class="num">requests</th><th class="num">errors</th></tr></thead>
<tbody id="ups"></tbody>
</table>
<h2>key spend today (usd)</h2>
<table>
<thead><tr><th>key</th><th class="num">spent</th><th class="num">daily cap</th></tr></thead>
<tbody id="keys"></tbody>
</table>
<div class="note">auto-refreshes every 5 seconds. data from /relay/status.</div>
<script>
function dot(state){
  if(state==="closed") return '<span class="dot ok"></span>';
  if(state==="open") return '<span class="dot bad"></span>';
  return '<span class="dot warn"></span>';
}
async function tick(){
  try{
    const r = await fetch("/relay/status");
    const s = await r.json();
    const mins = Math.floor(s.uptime_s/60), secs = s.uptime_s%60;
    document.getElementById("meta").textContent =
      "uptime "+mins+"m "+secs+"s | "+s.requests_total+" requests | $"+s.cost_total_usd.toFixed(4)+" total";
    document.getElementById("ups").innerHTML = s.upstreams.map(u =>
      "<tr><td>"+dot(u.breaker)+"</td><td>"+u.name+"</td><td>"+u.breaker+"</td>"+
      '<td class="num">'+u.avg_latency_ms+"</td>"+
      '<td class="num">'+u.requests+"</td>"+
      '<td class="num">'+u.errors+"</td></tr>").join("");
    const rows = s.keys_today.map(k =>
      "<tr><td>"+k.name+"</td>"+
      '<td class="num">'+k.spend_usd.toFixed(4)+"</td>"+
      '<td class="num">'+(k.daily_cap===null?"-":"$"+k.daily_cap.toFixed(2))+"</td></tr>");
    document.getElementById("keys").innerHTML =
      rows.length ? rows.join("") : '<tr><td colspan="3">no keys issued</td></tr>';
  }catch(e){
    document.getElementById("meta").textContent = "could not reach /relay/status";
  }
}
setInterval(tick, 5000);
tick();
</script>
</body>
</html>
"""
