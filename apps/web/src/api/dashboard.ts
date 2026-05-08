import { api } from './client'

export type ScoreDistItem = { range: string; count: number }
export type DimensionAvg = { name: string; avg_score: number }
export type DialogueTypeItem = { type: string; count: number }
export type ConcernTypeItem = { type: string; count: number }
export type DashboardBreakdownItem = {
  key: string
  label: string
  count: number
  task_count: number
  customer_count: number
  is_open_value: boolean
  distinct_value_count: number
  remaining_value_count: number
  department_code?: string | null
  department_name?: string | null
  indication_code?: string | null
  body_part_code?: string | null
  body_part_name?: string | null
  detail?: string | null
  value_breakdown?: DashboardBreakdownValueItem[]
}
export type DashboardBreakdownValueItem = {
  key: string
  label: string
  count: number
  task_count: number
  customer_count: number
}
export type VisitTrendItem = {
  week_start: string
  week_end: string
  week_label: string
  range_label: string
  count: number
}
export type ScoreTrendItem = {
  date: string
  label: string
  avg_score: number | null
  task_count: number
  dimension_averages: DimensionAvg[]
}
export type HospitalOptionItem = {
  hospital_code: string
  hospital_name: string
}
export type DashboardStaffOptionItem = {
  staff_id: string
  staff_name: string
  hospital_code: string | null
  job_label: string
}
export type StaffStatsItem = {
  staff_id: string
  staff_name: string
  hospital_code: string | null
  hospital_name: string | null
  job_label: string
  visit_count: number
  closed_won_count: number
  principal_amount: number
  recording_count: number
  linked_visit_count: number
  analyzed_count: number
  avg_score: number | null
  dimension_averages: DimensionAvg[]
}
export type ResultAnalysisModuleStats = {
  key: string
  label: string
  analyzed_count: number
  covered_count: number
  coverage_rate: number
  avg_item_count: number
}
export type ProcessEvaluationSummaryStats = {
  evaluated_count: number
  avg_total_score: number | null
  max_total_score: number
  pass_rate: number
  issue_count: number
  avg_passed_sections: number
}
export type ProcessEvaluationSectionStats = {
  code: string
  name: string
  evaluated_count: number
  avg_score: number | null
  max_score: number
  pass_count: number
  pass_rate: number
  issue_count: number
}
export type ProcessEvaluationIssueItem = {
  recording_id: string
  analysis_task_id: string
  file_name: string
  recorded_at: string | null
  staff_id: string | null
  staff_name: string | null
  section_code: string
  section_name: string
  checkpoint_code?: string | null
  checkpoint_name?: string | null
  description: string
  evidence?: string | null
}
export type RecentTask = {
  id: string
  file_name: string
  overall_score: number | null
  status: string
  created_at: string
}

export type DashboardExampleRecordingItem = {
  recording_id: string
  analysis_task_id: string
  file_name: string
  recorded_at: string | null
  duration_seconds: number | null
  staff_id: string | null
  staff_name: string | null
  total_score: number
  max_score: number
  indication_count: number
  tag_count: number
  concern_count: number
  summary: string
}

export type DashboardStats = {
  total_deal_amount: number
  total_closed_won_visits: number
  total_closed_won_customers: number
  total_tasks: number
  done_count: number
  running_count: number
  failed_count: number
  total_tag_count: number
  avg_tag_count: number
  total_indication_count: number
  avg_indication_count: number
  avg_score: number
  max_score: number
  min_score: number
  score_distribution: ScoreDistItem[]
  dimension_averages: DimensionAvg[]
  dialogue_types: DialogueTypeItem[]
  concern_types: ConcernTypeItem[]
  recent_low_scores: RecentTask[]
  positive_example_recordings: DashboardExampleRecordingItem[]
  negative_example_recordings: DashboardExampleRecordingItem[]
  // 业务统计
  total_customers: number
  total_visits: number
  visit_status_dist: { status: string; count: number }[]
  visit_trend: VisitTrendItem[]
  visit_trend_scope: 'staff' | 'hospital'
  visit_trend_hospital_code: string | null
  visit_trend_hospital_name: string | null
  visit_trend_can_select_hospital: boolean
  visit_trend_hospital_options: HospitalOptionItem[]
  score_trend: ScoreTrendItem[]
  dashboard_scope: 'all' | 'mine'
  dashboard_can_select_scope: boolean
  dashboard_can_select_hospital: boolean
  dashboard_hospital_code: string | null
  dashboard_hospital_name: string | null
  dashboard_hospital_options: HospitalOptionItem[]
  dashboard_can_select_staff: boolean
  dashboard_staff_id: string | null
  dashboard_staff_name: string | null
  dashboard_staff_options: DashboardStaffOptionItem[]
  staff_stats: StaffStatsItem[]
  score_staff_stats: StaffStatsItem[]
  total_recordings: number
  quality_passed_recordings: number
  recordings_with_visits: number
  recordings_uploaded: number
  recordings_transcribed: number
  // 转写 / 片段统计
  total_transcripts: number
  transcripts_completed: number
  transcripts_failed: number
  total_segments: number
  segments_with_visit: number
  tag_breakdown: DashboardBreakdownItem[]
  indication_breakdown: DashboardBreakdownItem[]
  result_analysis_modules: ResultAnalysisModuleStats[]
  process_evaluation_summary: ProcessEvaluationSummaryStats
  process_evaluation_sections: ProcessEvaluationSectionStats[]
  process_evaluation_issues: ProcessEvaluationIssueItem[]
}

export const fetchDashboard = (
  input?:
    | string
    | null
    | {
        hospital_code?: string | null
        scope_mode?: 'all' | 'mine'
        staff_id?: string | null
        date_from?: string
        date_to?: string
      },
  params?: {
    scope_mode?: 'all' | 'mine'
    staff_id?: string | null
    date_from?: string
    date_to?: string
  },
) => {
  const options =
    typeof input === 'object' && input !== null
      ? input
      : {
          hospital_code: input ?? undefined,
          scope_mode: params?.scope_mode,
          staff_id: params?.staff_id,
          date_from: params?.date_from,
          date_to: params?.date_to,
        }
  const searchParams = new URLSearchParams()
  if (options.hospital_code) searchParams.set('hospital_code', options.hospital_code)
  if (options.scope_mode) searchParams.set('scope_mode', options.scope_mode)
  if (options.staff_id) searchParams.set('staff_id', options.staff_id)
  if (options.date_from) searchParams.set('date_from', options.date_from)
  if (options.date_to) searchParams.set('date_to', options.date_to)

  return api
    .get('dashboard', {
      searchParams,
    })
    .json<DashboardStats>()
}
