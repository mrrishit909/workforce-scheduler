-- Domain model. Names follow the blueprint's persistence model; additions are marked (+).

CREATE TABLE locations (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, name text NOT NULL, timezone text NOT NULL, labor_rule_set_id uuid,
  size real NOT NULL, rule_pack text NOT NULL,                                           -- (+) staffing-standard scale; name of the rule pack in ws/rules.py
  UNIQUE (tenant_id, name));
SELECT enable_tenant_rls('locations');

CREATE TABLE workers (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, employee_number text NOT NULL, home_location_id uuid NOT NULL REFERENCES locations, employment_type text NOT NULL,
  hourly_cost numeric NOT NULL, max_weekly_hours numeric NOT NULL, active boolean NOT NULL DEFAULT true,
  name text NOT NULL, target_weekly_hours numeric NOT NULL, recent_weekend_shifts int NOT NULL DEFAULT 0,      -- (+) contracted hours; weekend shifts in the last four weeks
  UNIQUE (tenant_id, employee_number));
CREATE INDEX workers_location ON workers (home_location_id) WHERE active;
SELECT enable_tenant_rls('workers');

CREATE TABLE skills (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, name text NOT NULL, certification_required boolean NOT NULL, UNIQUE (tenant_id, name));
SELECT enable_tenant_rls('skills');
CREATE TABLE worker_skills (worker_id uuid REFERENCES workers ON DELETE CASCADE, skill_id uuid REFERENCES skills, proficiency int NOT NULL, expires_on date, PRIMARY KEY (worker_id, skill_id));

CREATE TABLE availability (id uuid PRIMARY KEY, worker_id uuid NOT NULL REFERENCES workers ON DELETE CASCADE, starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL,
  availability_type text NOT NULL CHECK (availability_type IN ('unavailable', 'preferred', 'avoid')), preference_weight numeric NOT NULL DEFAULT 1);
CREATE INDEX availability_worker ON availability (worker_id, starts_at);

CREATE TABLE demand_intervals (location_id uuid REFERENCES locations, skill_id uuid REFERENCES skills, starts_at timestamptz, ends_at timestamptz NOT NULL, required_workers numeric NOT NULL, forecast_version text,
  PRIMARY KEY (location_id, skill_id, starts_at, forecast_version));

-- (+) what the forecast learns from: one row per store and day, half-hourly transactions as an array, and the known drivers. Future days have drivers only.
CREATE TABLE demand_days (location_id uuid REFERENCES locations, day date, transactions int[], promotion boolean NOT NULL, rain boolean NOT NULL, PRIMARY KEY (location_id, day));

CREATE TABLE schedule_runs (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, horizon_start date NOT NULL, horizon_end date NOT NULL, objective_value numeric, solver_status text NOT NULL, runtime_ms int NOT NULL,
  parameters jsonb NOT NULL, location_id uuid NOT NULL REFERENCES locations, summary jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());      -- (+)
SELECT enable_tenant_rls('schedule_runs');

CREATE TABLE shifts (id uuid PRIMARY KEY, location_id uuid NOT NULL REFERENCES locations, starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL, required_skill_id uuid NOT NULL REFERENCES skills,
  status text NOT NULL CHECK (status IN ('assigned', 'called_out', 'cancelled')), source text NOT NULL,
  schedule_run_id uuid NOT NULL REFERENCES schedule_runs ON DELETE CASCADE);             -- (+)
CREATE INDEX shifts_run ON shifts (schedule_run_id, starts_at);
CREATE TABLE assignments (shift_id uuid REFERENCES shifts ON DELETE CASCADE, worker_id uuid REFERENCES workers, assigned_at timestamptz NOT NULL DEFAULT now(), assignment_cost numeric NOT NULL, overtime_hours numeric NOT NULL DEFAULT 0,
  PRIMARY KEY (shift_id, worker_id));
CREATE INDEX assignments_worker ON assignments (worker_id);

CREATE TABLE rule_violations (id uuid PRIMARY KEY, schedule_run_id uuid NOT NULL REFERENCES schedule_runs ON DELETE CASCADE, worker_id uuid REFERENCES workers, rule_code text NOT NULL, severity text NOT NULL, detail jsonb NOT NULL);

CREATE TABLE shift_market_orders (id uuid PRIMARY KEY, shift_id uuid NOT NULL REFERENCES shifts ON DELETE CASCADE, offered_by uuid NOT NULL REFERENCES workers, status text NOT NULL CHECK (status IN ('open', 'claimed', 'expired')),
  expires_at timestamptz NOT NULL, claimed_by uuid REFERENCES workers, decided_at timestamptz);      -- (+)
CREATE UNIQUE INDEX one_open_order_per_shift ON shift_market_orders (shift_id) WHERE status = 'open';

-- (+) a worker reporting they cannot work an assigned shift
CREATE TABLE callouts (id uuid PRIMARY KEY, shift_id uuid NOT NULL UNIQUE REFERENCES shifts ON DELETE CASCADE, worker_id uuid NOT NULL REFERENCES workers, reason text NOT NULL, reported_at timestamptz NOT NULL DEFAULT now(),
  status text NOT NULL CHECK (status IN ('open', 'covered', 'partly_covered')));
