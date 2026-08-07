/**
 * Degree progress, rendered so that the trap is impossible to fall into.
 *
 * PROJECT_PLAN Phase 6 calls this out: "surplus credits in one category don't
 * offset another" is the #1 thing students in the brief get wrong. Jad
 * (S2023027) is the dataset's proof -- 55 of 72 credits, which reads as 76%
 * complete, while holding 1 of 4 General Education courses and having never
 * started the 8-credit capstone category.
 *
 * So this component deliberately refuses to lead with the number that causes the
 * mistake:
 *
 * - The headline is **categories satisfied**, not credits. A student is finished
 *   when every category is individually met; the credit total is a consequence,
 *   not the test.
 * - The credit total is shown, but labelled as *not* the graduation test, and it
 *   is never rendered as a single progress bar. One bar across 72 credits is
 *   precisely the picture that lies.
 * - Surplus credits are drawn **outside** the category's bar, in a detached
 *   chip, and captioned with where they cannot go. Rendering them inside the bar
 *   would say the opposite of the rule.
 * - An untouched category is given the loudest treatment on the page, because it
 *   is the thing a student most needs to see and the thing a percentage hides.
 */

import type { CategoryProgress, DegreeProgress } from "../api";

function ruleText(category: CategoryProgress): string {
  return category.selection_rule === "ALL"
    ? `all ${category.courses_offered} courses required`
    : `any ${category.courses_required} of ${category.courses_offered} courses`;
}

function CategoryRow({ category }: { category: CategoryProgress }) {
  const applied = category.credits_applied;
  const required = category.credits_required;
  const surplus = Math.max(0, category.credits_counted - required);
  const percent = required === 0 ? 0 : Math.min(100, (applied / required) * 100);
  const inProgressPercent =
    required === 0
      ? 0
      : Math.min(100 - percent, (category.credits_in_progress / required) * 100);

  const state = category.is_satisfied
    ? "satisfied"
    : applied === 0
      ? "untouched"
      : "partial";

  return (
    <article className={`category category--${state}`}>
      <header className="category__head">
        <h4>{category.category_name}</h4>
        <span className={`badge badge--${state}`}>
          {state === "satisfied"
            ? "Satisfied"
            : state === "untouched"
              ? "Not started"
              : `${category.credits_remaining} credits short`}
        </span>
      </header>

      <div className="category__bar" role="img"
        aria-label={`${applied} of ${required} credits applied`}>
        <div className="category__fill" style={{ width: `${percent}%` }} />
        {inProgressPercent > 0 && (
          <div
            className="category__fill category__fill--pending"
            style={{ width: `${inProgressPercent}%` }}
          />
        )}
      </div>

      <div className="category__meta">
        <span>
          <strong>
            {applied} / {required}
          </strong>{" "}
          credits
        </span>
        <span className="muted">{ruleText(category)}</span>
        <span className="muted">
          {category.courses_counted} of {category.courses_offered} courses counted
        </span>
        {category.credits_in_progress > 0 && (
          <span className="muted">
            {category.credits_in_progress} in progress
          </span>
        )}
        {category.min_grade_points && (
          <span className="muted">
            needs C&minus; or above ({category.min_grade_points})
          </span>
        )}
      </div>

      {/* Deliberately outside the bar. Drawn inside, it would read as progress
          that counts somewhere -- which is exactly the rule being broken. */}
      {surplus > 0 && (
        <p className="surplus">
          <span className="surplus__chip">+{surplus} surplus</span>
          Earned beyond this category&rsquo;s requirement. These credits do
          <strong> not</strong> count toward any other category.
        </p>
      )}
    </article>
  );
}

export function DegreeProgressPanel({ progress }: { progress: DegreeProgress }) {
  const satisfied = progress.categories.filter((c) => c.is_satisfied).length;
  const total = progress.categories.length;

  return (
    <section className="panel">
      <header className="progress__headline">
        <div>
          <p className="eyebrow">Requirement categories satisfied</p>
          <p className="headline-number">
            {satisfied} <span className="muted">of {total}</span>
          </p>
          <p className="muted">
            {progress.program_name} ({progress.program_code})
          </p>
        </div>

        <div className="progress__credits">
          <p className="eyebrow">Credits earned</p>
          <p className="headline-number headline-number--secondary">
            {progress.credits_earned}{" "}
            <span className="muted">of {progress.total_credits_required}</span>
          </p>
          {/* The whole point of this caption. */}
          <p className="warning-note">
            This total is <strong>not</strong> the graduation test. Every
            category must be satisfied individually, and surplus credits in one
            never make up a shortfall in another.
          </p>
        </div>
      </header>

      {progress.all_categories_satisfied ? (
        <p className="callout callout--good">
          Every requirement category is satisfied. Remaining graduation
          conditions &mdash; GPA, C&minus; in every Major Core course, placement
          and capstone, finances, and the application deadline &mdash; are set out
          in the Student Handbook, section 3.
        </p>
      ) : (
        <p className="callout callout--warn">
          Still to satisfy: <strong>{progress.unsatisfied_categories.join(", ")}</strong>.
        </p>
      )}

      <div className="category-list">
        {progress.categories.map((category) => (
          <CategoryRow key={category.category_id} category={category} />
        ))}
      </div>
    </section>
  );
}
