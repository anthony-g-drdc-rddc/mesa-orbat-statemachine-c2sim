// Optional compatibility check against an unchanged upstream source checkout.
// Run from a temporary Node project containing the dependencies listed in docs/architecture.md.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const require = createRequire(path.join(process.cwd(), 'package.json'));
const {build} = require('esbuild');
const [checkout, ...inputs] = process.argv.slice(2);
if (!checkout || !inputs.length) throw new Error('Usage: node verify_orbat_native.mjs UPSTREAM_CHECKOUT SCENARIO...');
const root = path.resolve(checkout);
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'c2sim-orbat-check-'));
try {
  const bundle = path.join(temp, 'native.mjs');
  await build({stdin:{contents:`
    export {upgradeScenarioIfNecessary} from ${JSON.stringify(root+'/src/scenariostore/upgrade.ts')};
    export {unitStateToInternal,unitResourcesToInternal} from ${JSON.stringify(root+'/src/scenariostore/scenarioCodec.ts')};
    export {applyResourceUpdate,applyResourceDiff} from ${JSON.stringify(root+'/src/scenariostore/unitResources.ts')};`,resolveDir:process.cwd()},
    bundle:true, platform:'node',format:'esm',outfile:bundle,
    alias:{'@':root+'/src'},nodePaths:[path.join(process.cwd(),'node_modules')]});
  const codec = await import(pathToFileURL(bundle));
  for (const file of inputs) {
    const document = codec.upgradeScenarioIfNecessary(JSON.parse(fs.readFileSync(file, 'utf8')));
    const traverse = units => units.flatMap(u => [u, ...traverse(u.subUnits || [])]);
    const units = document.sides.flatMap(s => [...traverse(s.subUnits || []), ...s.groups.flatMap(g => traverse(g.subUnits || []))]);
    assert.equal(units.length, 8);
    assert.equal(document.layerStack[0].items.length, 4);
    for (const unit of units) {
      const names = Object.fromEntries(['equipment','personnel','supplies'].map(c => [c,Object.fromEntries((unit[c]||[]).map(r=>[r.name,r.name]))]));
      const resources = codec.unitResourcesToInternal(unit,{equipment:n=>n,personnel:n=>n,supplies:n=>n});
      for (const state of unit.state || []) {
        const internal = codec.unitStateToInternal(state,names);
        for (const category of ['equipment','personnel','supplies']) {
          codec.applyResourceUpdate(resources[category],internal.update?.[category],category);
          codec.applyResourceDiff(resources[category],internal.diff?.[category],category);
        }
      }
      if (unit.id === 'POST-1' && unit.state?.length > 1) {
        assert.equal(resources.supplies.find(r=>r.id==='supply').onHand,50);
      }
    }
    console.log(JSON.stringify({file,units:8,routes:4,upstreamNativeCodec:'passed'}));
  }
} finally {
  fs.rmSync(temp,{recursive:true,force:true});
}
