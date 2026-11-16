# Async report_metrics (kit patch v2, job side): run by async_job.sh as a separate openroad process.
# Parameters in $ORFS_ASYNC_PARAMS (written by async_reports.tcl): kind (report | irdrop), odb and sdc (relative to
# RESULTS_DIR), parasitics (none | placement | global_routing | spef), metrics_stage, prewarm (0 | 1), args.
# prewarm 1: the inline call ran in a process that had already propagated power activities, so the job does that
# once before the report section, outside any report redirection (STA-2402 is reported only by the first
# propagation of a process). The log marks the report section with ORFS_ASYNC_JOB section_begin / section_end;
# async_reports_merge.py counts the warnings of that section only.
source $::env(SCRIPTS_DIR)/load.tcl
source $::env(ORFS_ASYNC_PARAMS)
if { $::async(metrics_stage) ne "" } {
  utl::set_metrics_stage $::async(metrics_stage)
}
load_design $::async(odb) $::async(sdc)
switch -- $::async(parasitics) {
  placement { log_cmd estimate_parasitics -placement }
  global_routing { log_cmd estimate_parasitics -global_routing }
  spef { log_cmd read_spef $::env(RESULTS_DIR)/6_final.spef }
  none { }
  default { error "unknown parasitics source $::async(parasitics)" }
}
puts "ORFS_ASYNC_JOB parasitics_requested=$::async(parasitics) parasitics_now=[est::parasitics_source]"
if { $::async(prewarm) } {
  array set ::async_scene_keys {}
  if { [env_var_exists_and_non_empty CORNERS] } {
    # report_metrics reports per corner; the first propagation of the process is the one that warns
    set ::async_scene_keys(-scene) [lindex $::env(CORNERS) 0]
  }
  sta::design_power [sta::parse_scene ::async_scene_keys]
  puts "ORFS_ASYNC_JOB prewarm done"
}
puts "ORFS_ASYNC_JOB section_begin"
flush stdout
if { $::async(kind) eq "report" } {
  report_metrics {*}$::async(args)
} elseif { $::async(kind) eq "irdrop" } {
  set rest [lassign $::async(args) volts]
  foreach { net voltage } $volts {
    set_pdnsim_net_voltage -net $net -voltage $voltage
  }
  analyze_power_grid {*}$rest
} else {
  error "unknown job kind $::async(kind)"
}
puts "ORFS_ASYNC_JOB section_end"
puts "ORFS_ASYNC_JOB done"
