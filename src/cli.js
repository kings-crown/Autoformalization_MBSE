import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { runPipeline } from './pipeline.js';

function usage() {
  console.error('Usage: npm run pipeline:run -- --input <file> --output <dir> [--skip-solver]');
  process.exit(1);
}

function parseArgs(argv) {
  const args = {
    skipSolver: process.env.SKIP_Z3 === '1'
  };

  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === '--input') {
      args.input = argv[++i];
    } else if (token === '--output') {
      args.output = argv[++i];
    } else if (token === '--skip-solver') {
      args.skipSolver = true;
    } else if (token === '--model') {
      args.model = argv[++i];
    } else if (token === '--help' || token === '-h') {
      usage();
    } else {
      console.warn(`Unknown argument: ${token}`);
    }
  }

  if (!args.input || !args.output) {
    usage();
  }

  return args;
}

async function main() {
  const argv = process.argv.slice(2);
  const args = parseArgs(argv);

  const inputPath = path.resolve(args.input);
  const outputDir = path.resolve(args.output);

  const raw = await readFile(inputPath, 'utf8');
  const requirementsDoc = JSON.parse(raw);

  const result = await runPipeline({
    requirements: requirementsDoc,
    options: {
      model: args.model,
      skipSolver: args.skipSolver
    }
  });

  await mkdir(outputDir, { recursive: true });
  const baseName = `${requirementsDoc.id || 'requirements'}`;

  const tlfPath = path.join(outputDir, `${baseName}.tlf.json`);
  const smtPath = path.join(outputDir, `${baseName}.smt2`);
  const metaPath = path.join(outputDir, `${baseName}.solver.json`);

  await writeFile(tlfPath, JSON.stringify(result.logicalForm, null, 2), 'utf8');
  await writeFile(smtPath, `${result.smtlib}\n`, 'utf8');

  if (result.solver) {
    await writeFile(metaPath, JSON.stringify(result.solver, null, 2), 'utf8');
  }

  const rel = path.relative(path.dirname(fileURLToPath(import.meta.url)), outputDir) || '.';

  console.log(`Typed logical form written to ${tlfPath}`);
  console.log(`SMT-LIB script written to ${smtPath}`);
  if (result.solver?.status === 'ok') {
    console.log(`Solver completed with status: ${result.solver.result}`);
  } else if (result.solver) {
    console.log(`Solver step: ${result.solver.status} (${result.solver.reason || result.solver.error || 'see solver metadata'})`);
  }
}

main().catch((error) => {
  console.error('Pipeline failed:', error);
  process.exitCode = 1;
});
