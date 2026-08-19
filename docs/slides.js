const slides=[...document.querySelectorAll('.slide')];
const counter=document.querySelector('#counter');
const progress=document.querySelector('#progress');
const slideTitle=document.querySelector('#slideTitle');
let current=0;

function show(index,{updateHash=true}={}){
  current=Math.max(0,Math.min(slides.length-1,index));
  slides.forEach((slide,i)=>slide.classList.toggle('active',i===current));
  counter.textContent=`${String(current+1).padStart(2,'0')} / ${String(slides.length).padStart(2,'0')}`;
  progress.style.width=`${((current+1)/slides.length)*100}%`;
  slideTitle.textContent=slides[current].dataset.title;
  document.querySelector('#prev').disabled=current===0;
  document.querySelector('#next').disabled=current===slides.length-1;
  document.title=`${current+1}. ${slides[current].dataset.title} · IoT Edge Flood Warning`;
  if(updateHash)history.replaceState(null,'',`#${current+1}`);
}

document.querySelector('#prev').addEventListener('click',()=>show(current-1));
document.querySelector('#next').addEventListener('click',()=>show(current+1));
document.querySelector('#fullscreen').addEventListener('click',()=>document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen());
document.addEventListener('keydown',event=>{
  if(['ArrowRight','PageDown',' ','Enter'].includes(event.key)){event.preventDefault();show(current+1)}
  if(['ArrowLeft','PageUp','Backspace'].includes(event.key)){event.preventDefault();show(current-1)}
  if(event.key==='Home')show(0);
  if(event.key==='End')show(slides.length-1);
  if(event.key.toLowerCase()==='f')document.querySelector('#fullscreen').click();
});
let touchX=0;
document.addEventListener('touchstart',event=>touchX=event.changedTouches[0].screenX,{passive:true});
document.addEventListener('touchend',event=>{const delta=event.changedTouches[0].screenX-touchX;if(Math.abs(delta)>60)show(current+(delta<0?1:-1))},{passive:true});
window.addEventListener('hashchange',()=>show(Number(location.hash.slice(1)||1)-1,{updateHash:false}));
show(Number(location.hash.slice(1)||1)-1,{updateHash:false});
