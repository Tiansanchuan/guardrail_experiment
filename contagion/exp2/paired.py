import json, sys, random, collections
eps = [json.loads(l) for l in open(sys.argv[1])]
print(len(eps), 'episodes |', dict(collections.Counter(e['condition'] for e in eps)))
print('ended:', dict(collections.Counter(e['ended'] for e in eps)))
late = collections.defaultdict(lambda: collections.defaultdict(list))
for e in eps:
    late[e['task_id']][e['condition']].append(1.0 if e['passed'] and (e.get('round_passed') or 0) >= 2 else 0.0)
def diff(a, b):
    ts = [t for t in late if late[t][a] and late[t][b]]
    d = [sum(late[t][a]) / len(late[t][a]) - sum(late[t][b]) / len(late[t][b]) for t in ts]
    random.seed(0)
    bs = sorted(sum(random.choices(d, k=len(d))) / len(d) for _ in range(2000))
    return len(ts), 100 * sum(d) / len(d), 100 * bs[50], 100 * bs[1949]
for a, b in [('cheat_pass', 'placebo'), ('cheat_pass', 'honest'), ('cheat_caught', 'placebo'), ('cheat_caught', 'honest'), ('cheat_pass', 'cheat_caught')]:
    n, m, lo, hi = diff(a, b)
    print(f'{a:13s} vs {b:13s} tasks={n:3d}  {m:+5.1f} pts  95% CI [{lo:+5.1f}, {hi:+5.1f}]')
