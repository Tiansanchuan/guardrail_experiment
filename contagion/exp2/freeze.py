import json, sys, collections
src, out = sys.argv[1], sys.argv[2]
eps = [json.loads(l) for l in open(src)]
by = collections.defaultdict(list)
for e in eps:
    by[(e['task_id'], e['condition'])].append(e)
pairs, bad, per_cond = 0, 0, collections.Counter()
with open(out, 'w') as f:
    for (task, cond), group in by.items():
        cheat = [e for e in group if e['cheated'] and e.get('round_passed')]
        clean = [e for e in group if not e['passed']]
        if not cheat or not clean:
            continue
        c, h = cheat[0], clean[0]
        k = c['round_passed']
        if c['messages'][2 * k]['role'] != 'assistant' or len(h['messages']) <= 2 * k:
            bad += 1; continue
        for label, e in (('cheated', c), ('did_not_cheat', h)):
            f.write(json.dumps({'task_id': task, 'condition': cond, 'label': label, 'sample': e['sample'],
                                'fork_round': k, 'context': e['messages'][:2 * k],
                                'next_reply': e['messages'][2 * k]['content']}) + '\n')
        pairs += 1; per_cond[cond] += 1
print(pairs, 'pairs written to', out, '| skipped:', bad)
print(dict(per_cond))
