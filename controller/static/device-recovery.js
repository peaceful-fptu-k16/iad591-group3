const $=selector=>document.querySelector(selector);
const esc=value=>String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
let devices=[];
let toastTimer;

function toast(message){const element=$('#toast');element.textContent=message;element.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>element.classList.remove('show'),3200)}
async function api(path,options={}){const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail||`HTTP ${response.status}`)}return response.json()}

async function loadDevices(){try{const [health,items]=await Promise.all([api('/health'),api('/api/devices')]);devices=items;$('#factory-reset-device-options').innerHTML=devices.map(device=>`<option value="${esc(device.device_id)}">${esc(device.label)}</option>`).join('');const ready=Boolean(health.mqtt_connected);$('#factory-reset-submit').disabled=!ready;$('#recovery-status').textContent=ready?`MQTT đã kết nối · ${devices.length} node trong database`:'MQTT đang mất kết nối; chưa thể gửi lệnh.'}catch(error){$('#factory-reset-submit').disabled=true;$('#recovery-status').textContent=`Không kết nối được controller: ${error.message}`}}

$('#factory-reset-form').addEventListener('submit',async event=>{event.preventDefault();const input=$('#factory-reset-device-id');const deviceId=input.value.trim();if(!input.reportValidity())return;const known=devices.find(device=>device.device_id===deviceId);const label=known?`${known.label} (${deviceId})`:deviceId;if(!confirm(`Factory reset ${label}?\n\nESP32 sẽ xóa Wi-Fi và đăng ký, reboot rồi mở WaterSensor-Setup.`))return;const button=$('#factory-reset-submit');button.disabled=true;button.textContent='Đang gửi lệnh…';try{const result=await api(`/api/devices/${encodeURIComponent(deviceId)}/factory-reset`,{method:'POST'});input.value='';toast(`Đã gửi factory reset tới ${result.device_id}`);await loadDevices()}catch(error){toast(error.message)}finally{button.textContent='Gửi factory reset'}});

loadDevices();
