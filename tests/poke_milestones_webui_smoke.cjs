const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL}=require('node:url');
// Optional desktop integration smoke test; no real AstrBot or QQ calls.
const puppeteer = process.env.HALLMARK_PUPPETEER
  ? require(path.join(process.env.HALLMARK_PUPPETEER,'node_modules','puppeteer-core'))
  : require('puppeteer-core');
(async()=>{
  const browser=await puppeteer.launch({executablePath:process.env.CHROME_PATH,headless:true});
  try {
    const page=await browser.newPage(); await page.setViewport({width:1280,height:800,deviceScaleFactor:1});
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.evaluateOnNewDocument(()=>{
      let state={enabled:false,rules:[],history:[]};
      let fail=false,malformed=false,loadFail=false,listener;
      window.qa={
        state:()=>structuredClone(state),fail:()=>fail=true,malformed:()=>malformed=true,
        loadFail:()=>loadFail=true,history:row=>state.history=[row],dark:value=>listener({isDark:value})
      };
      window.AstrBotPluginView={
        ready:async()=>{},onContext:cb=>{listener=cb;cb({isDark:false});},
        apiGet:async route=>{if(route!=='poke-milestones/state')throw Error(route);
          if(loadFail){loadFail=false;throw Error('mock read failure');} return structuredClone(state);},
        apiPost:async(route,body)=>{
          await new Promise(r=>setTimeout(r,80));
          if(fail){fail=false;throw Error('mock write failure');}
          if(malformed){malformed=false;return {data:{rules:[]}};}
          switch(route){
            case 'poke-milestones/settings':state.enabled=body.enabled;break;
            case 'poke-milestones/rules/save':{
              const rule={...body,id:body.id||'test-1'},i=state.rules.findIndex(r=>r.id===rule.id);
              if(i<0)state.rules.push(rule);else state.rules[i]=rule;break;
            }
            case 'poke-milestones/rules/toggle':state.rules.find(r=>r.id===body.id).enabled=body.enabled;break;
            case 'poke-milestones/rules/delete':state.rules=state.rules.filter(r=>r.id!==body.id);break;
            default:throw Error(route);
          }return structuredClone(state);
        }
      };
    });
    await page.goto(pathToFileURL(path.join(process.cwd(),'views','poke-milestones','index.html')).href);
    const wait=fn=>page.waitForFunction(fn,{timeout:10000});
    const val=sel=>page.$eval(sel,el=>el.value);
    const fill=async(sel,value)=>{await page.locator(sel).fill(value);};
    const shot=name=>process.env.QA_SCREENSHOT_DIR ? page.screenshot({path:path.join(process.env.QA_SCREENSHOT_DIR,name+'.png')}) : Promise.resolve();
    await wait(()=>!document.querySelector('#add').disabled);
    assert.equal(await page.$eval('#global',el=>el.checked),false);
    assert.equal(await page.$$('tbody tr').then(r=>r.length),0);
    await shot('desktop-empty');
    await page.click('#add');
    await wait(()=>document.querySelector('#drawer').open);
    assert.equal(await val('input[name="target"]'),'30');
    assert.equal(await val('input[name="tolerance"]'),'2');
    assert.equal(await val('input[name="mute_minutes"]'),'1');
    assert.equal(await page.$eval('input[name="llm_enabled"]',el=>el.checked),false);
    const name='Test <img src=x onerror=alert(1)>';
    await fill('input[name="name"]',name);
    await fill('textarea[name="prompt"]','gentle teasing');
    await page.click('input[name="llm_enabled"]');
    await shot('desktop-editor');
    await page.click('#save');
    await wait(()=>!document.querySelector('#drawer').open&&document.querySelector('.rule-name')?.textContent.includes('<img'));
    assert.equal(await page.$$('#rules-panel img').then(r=>r.length),0);
    assert.equal(await page.evaluate(()=>qa.state().rules[0].name),name);
    await page.click('#global');
    await wait(()=>document.querySelector('#global').checked&&!document.querySelector('#global').disabled);
    assert.equal(await page.evaluate(()=>qa.state().enabled),true);
    await page.click('[data-edit="test-1"]');
    await fill('input[name="name"]','Night rule');
    await fill('input[name="mute_minutes"]','3');
    await page.evaluate(()=>qa.fail());
    await page.click('#save');
    await wait(()=>!document.querySelector('#form-error').hidden&&!document.querySelector('#save').disabled);
    assert.equal(await page.evaluate(()=>qa.state().rules[0].mute_minutes),1);
    assert.equal(await page.$eval('#drawer',el=>el.open),true);
    await page.click('#save');
    await wait(()=>!document.querySelector('#drawer').open&&document.querySelector('.rule-name')?.textContent==='Night rule');
    assert.equal(await page.evaluate(()=>qa.state().rules[0].mute_minutes),3);
    await page.click('[data-toggle="test-1"]');
    await wait(()=>qa.state().rules[0].enabled===false&&!document.querySelector('[data-toggle]').disabled);
    await page.click('[data-toggle="test-1"]');
    await wait(()=>qa.state().rules[0].enabled===true&&!document.querySelector('[data-toggle]').disabled);
    await page.evaluate(()=>qa.malformed());
    await page.click('#global');
    await wait(()=>document.querySelector('#message').classList.contains('error')&&!document.querySelector('#global').disabled);
    assert.equal(await page.$eval('#global',el=>el.checked),true);
    assert.equal(await page.$eval('.rule-name',el=>el.textContent),'Night rule');
    await page.evaluate(()=>qa.history({created_at:'2026-11-27T18:30:01+08:00',group_id:'100',user_id:'200',rule_name:'Night rule',count:30,status:'success',detail:''}));
    await page.click('#refresh');
    await wait(()=>document.querySelector('#history-panel').textContent.includes('2026-11-27 18:30:01'));
    assert.equal(await page.$eval('.result.success',el=>el.textContent.length>0),true);
    await page.evaluate(()=>qa.dark(true));
    await wait(()=>document.documentElement.dataset.theme==='dark');
    await shot('desktop-dark');
    await page.evaluate(()=>qa.dark(false));
    await shot('desktop-populated');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth===window.innerWidth),true);
    await page.click('[data-edit="test-1"]');
    await page.keyboard.press('Escape');
    await wait(()=>!document.querySelector('#drawer').open);
    page.once('dialog',d=>d.accept());
    await page.click('[data-delete="test-1"]');
    await wait(()=>qa.state().rules.length===0&&!document.querySelector('[data-delete]'));
    await page.evaluate(()=>qa.loadFail());
    await page.click('#refresh');
    await wait(()=>document.querySelector('#message').classList.contains('error'));
    await page.click('#refresh');
    await wait(()=>document.querySelector('#message').hidden&&!document.querySelector('#add').disabled);
    assert.deepEqual(errors,[]);
    console.log('PASS: 1280x800 desktop initial load, add/edit/toggle/delete, global toggle, rejected write, malformed response, safe literal rendering, history timestamp, theme, Escape, refresh recovery, no JS errors.');
  } finally {await browser.close();}
})().catch(err=>{console.error(err);process.exit(1)});
