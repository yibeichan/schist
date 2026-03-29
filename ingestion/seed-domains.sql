-- Seed research_domains taxonomy
-- Run after schema.sql. Safe to re-run (INSERT OR IGNORE).

INSERT OR IGNORE INTO research_domains (slug, label, description, parent_slug) VALUES
  ('ai',             'Artificial Intelligence',  'ML, LLMs, agents, alignment',                    NULL),
  ('ai-ml',          'Machine Learning',         'Supervised/unsupervised learning, deep learning', 'ai'),
  ('ai-agents',      'AI Agents',                'Multi-agent systems, tool use, planning',         'ai'),
  ('ai-alignment',   'AI Alignment',             'Safety, RLHF, constitutional AI',                'ai'),
  ('neuroscience',   'Neuroscience',             'Brain function, cognitive science, neuroimaging', NULL),
  ('neuro-imaging',  'Neuroimaging',             'fMRI, EEG, MEG, HBCD',                           'neuroscience'),
  ('neuro-cog',      'Cognitive Neuroscience',   'Memory, attention, learning',                     'neuroscience'),
  ('health',         'Health & Wellness',        'Personal health, fitness, sleep, nutrition',      NULL),
  ('health-bjj',     'BJJ & Martial Arts',       'Training, technique, competition',                'health'),
  ('product',        'Product & Engineering',    'Software, systems, architecture',                 NULL),
  ('product-rollup', 'RollUp',                   'RollUp platform — BJJ school gamification',       'product'),
  ('product-infra',  'Infrastructure',           'DevOps, CI/CD, deployment',                       'product'),
  ('operations',     'Operations',               'Team coordination, workflows, tooling',           NULL),
  ('ops-memory',     'Agent Memory & State',     'Memory architecture, Schist, agent coordination', 'operations'),
  ('ops-security',   'Security',                 'Auth, secrets, CSO reviews',                      'operations');
