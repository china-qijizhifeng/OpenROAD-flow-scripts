# Async report_metrics (kit patch v2, main flow side). Sourced at the end of load.tcl only when
# ORFS_ASYNC_REPORTS=1; without it the flow is the timing-patch flow unchanged.
#
# report_metrics calls become background jobs: a separate openroad process (async_report.tcl) loads an ODB and
# an SDC of the same design state, restores the parasitics the inline call had, and runs report_metrics with the
# same arguments into the same report file and metrics stage.
#   step-end calls      deferred until the step's orfs_write_db, then reported from that ODB
#                       (3_3 global place, 3_4 resizer, 3_5 detailed place, 4_1 cts final)
#   mid-step calls      written to a snapshot ODB at the call and reported from it (4_1 cts pre-repair-timing);
#                       ORFS_ASYNC_MIDSTEP=inline keeps them in the main flow
#   6_report "finish"   reported from 6_final.odb + 6_final.spef as soon as it is called
#   analyze_power_grid  with ORFS_ASYNC_FINISH_PARALLEL=1, one job per net (6_final.odb + SPEF), so the VDD and VSS
#                       IR drop and the finish report run side by side
#   in the main flow    floorplan final (its report_power result depends on the process state: register activity
#                       propagation does not converge, STA-2402) and every call on global-routing parasitics (a
#                       reloaded ODB rebuilds them from the guides and does not reproduce the in-memory routes)
#
# Equivalence with the inline flow
#   report files  STA-2402 is reported once per process, by the first activity propagation. The main flow tracks
#                 whether its process has propagated activities before each call (report_metrics, report_power,
#                 analyze_power_grid); when it has, the job propagates once before the report, outside any report
#                 redirection, so the warning stays out of the report file as it does inline.
#   metrics       each call leaves a placeholder metric "orfs_async__slot:<job>" at its position in the step's
#                 metrics; flow/util/async_reports_merge.py replaces it with the job's metrics after the join and
#                 adds the warnings the job's report section raised to the step's warning counts.
# Jobs live in $RESULTS_DIR/async_reports/<name>.{params.tcl,sdc,odb,pid,log,json,time,start,end,exit}. Each job is
# started with "bash $ORFS_ASYNC_JOB_SH <dir> <name>" (default scripts/async_job.sh), which writes <name>.exit last;
# the spawn records the launcher pid in <name>.pid. "make finish" (and do-finish) ends with the target
# async_reports_join: flow/util/async_reports_merge.py --wait waits until every job has its .exit (or its
# launcher is gone), then merges, and fails when a job failed. Each spawn prints an ORFS_ASYNC line.

set ::orfs_async_dir [file join $::env(RESULTS_DIR) async_reports]
file mkdir $::orfs_async_dir
set ::orfs_async_pending {}
set ::orfs_async_power_used 0
array set ::orfs_async_volts {}
if { ![info exists ::orfs_async_seq] } {
  set ::orfs_async_seq 0
}

# The metrics stage each step script sets (utl::set_metrics_stage runs before load.tcl, so it is not observable
# here). The merge checks every job's keys against the stage of its placeholder.
proc ::orfs_async_metrics_stage { mid } {
  set step [expr { [info exists ::env(ORFS_STEP)] ? $::env(ORFS_STEP) : "" }]
  switch -- $step {
    2_1_floorplan { set s "floorplan__{}" }
    3_3_place_gp { set s "globalplace__{}" }
    3_4_place_resized { set s "placeopt__{}" }
    3_5_place_dp { set s "detailedplace__{}" }
    4_1_cts { set s [expr { $mid ? "cts__{}__pre_repair_timing" : "cts__{}" }] }
    5_1_grt { set s "globalroute__{}" }
    6_report { set s "finish__{}" }
    default { set s "" }
  }
  return $s
}

proc ::orfs_async_kind { stage when } {
  set step_end {
    {3 {global place}} {3 resizer} {3 {detailed place}} {4 {cts final}} {5 {global route}}
  }
  set mid {
    {4 {cts pre-repair-timing}} {5 {global route pre repair design}} {5 {global route post repair design}}
    {5 {global route post repair timing}} {5 {global route post repair timing_opt_wns}}
  }
  if { [lsearch -exact $step_end [list $stage $when]] >= 0 } {
    return step_end
  }
  if { [lsearch -exact $mid [list $stage $when]] >= 0 } {
    return mid
  }
  if { $stage == 6 && $when == "finish" } {
    return finish
  }
  return other
}

proc ::orfs_async_name { what } {
  incr ::orfs_async_seq
  return "$::orfs_t::step-[format %02d $::orfs_async_seq]-[string map {{ } _} $what]"
}

# Placeholder at the position the inline call would have written its metrics.
proc ::orfs_async_slot { name } {
  utl::metric_integer "orfs_async__slot:$name" 0
}

# Write the job parameters and start the job. odb is relative to RESULTS_DIR.
proc ::orfs_async_spawn { name kind odb parasitics metrics_stage prewarm args } {
  set dir $::orfs_async_dir
  # a rerun of the step starts the job again: the join must not see the previous run's status
  file delete -force [file join $dir $name.exit] [file join $dir $name.end] [file join $dir $name.pid]
  set sdc async_reports/$name.sdc
  write_sdc -no_timestamp [file join $::env(RESULTS_DIR) $sdc]
  set f [open [file join $dir $name.params.tcl] w]
  puts $f [list set ::async(kind) $kind]
  puts $f [list set ::async(odb) $odb]
  puts $f [list set ::async(sdc) $sdc]
  puts $f [list set ::async(parasitics) $parasitics]
  puts $f [list set ::async(metrics_stage) $metrics_stage]
  puts $f [list set ::async(prewarm) $prewarm]
  puts $f [list set ::async(args) $args]
  close $f
  set pid [exec bash $::env(ORFS_ASYNC_JOB_SH) $dir $name < /dev/null > /dev/null 2>@1 &]
  set f [open [file join $dir $name.pid] w]
  puts $f $pid
  close $f
  puts "ORFS_ASYNC spawn step=$::orfs_t::step name=$name kind=$kind odb=$odb parasitics=$parasitics\
    prewarm=$prewarm pid=$pid t_us=[clock microseconds] args=$args"
  flush stdout
}

::orfs_t::wrap orfs_async_spawn

proc ::orfs_async_snapshot { odb } {
  write_db [file join $::env(RESULTS_DIR) $odb]
}
::orfs_t::wrap orfs_async_snapshot

rename ::report_metrics ::orfs_async_inline_report_metrics
proc ::report_metrics { stage when { include_erc true } { include_clock_skew true } } {
  if { $::env(SKIP_REPORT_METRICS) } {
    return
  }
  set kind [::orfs_async_kind $stage $when]
  set src [est::parasitics_source]
  if { $src eq "global_routing" && $kind ne "other" } {
    # Global-routing parasitics are rebuilt from the dbGuides after a reload and do not reproduce the in-memory
    # routes (measured on gf180 aes: 5_global_route.rpt tns -106.26 inline, -106.18 from 5_1_grt.odb), so stay inline.
    set kind global_routing
  }
  if { $kind eq "mid" && [info exists ::env(ORFS_ASYNC_MIDSTEP)] && $::env(ORFS_ASYNC_MIDSTEP) eq "inline" } {
    set kind mid_inline
  }
  set prewarm $::orfs_async_power_used
  set ::orfs_async_power_used 1
  switch -- $kind {
    step_end {
      set name [::orfs_async_name $when]
      ::orfs_async_slot $name
      lappend ::orfs_async_pending [list $name $stage $when $include_erc $include_clock_skew $src $prewarm]
      puts "ORFS_ASYNC defer step=$::orfs_t::step name=$name stage=$stage when=$when parasitics=$src prewarm=$prewarm"
      return
    }
    finish {
      set name [::orfs_async_name $when]
      ::orfs_async_slot $name
      ::orfs_async_spawn $name report 6_final.odb $src [::orfs_async_metrics_stage 0] $prewarm \
        $stage $when $include_erc $include_clock_skew
      return
    }
    mid {
      set name [::orfs_async_name $when]
      ::orfs_async_snapshot async_reports/$name.odb
      ::orfs_async_slot $name
      ::orfs_async_spawn $name report async_reports/$name.odb $src \
        [::orfs_async_metrics_stage [expr { $stage == 4 }]] $prewarm $stage $when $include_erc $include_clock_skew
      return
    }
  }
  puts "ORFS_ASYNC inline step=$::orfs_t::step stage=$stage when=$when kind=$kind"
  ::orfs_async_inline_report_metrics $stage $when $include_erc $include_clock_skew
}

# report_power outside report_metrics (e.g. recover_power_helper) also propagates activities in this process.
rename ::report_power ::orfs_async_orig_report_power
proc ::report_power { args } {
  set ::orfs_async_power_used 1
  uplevel 1 [list ::orfs_async_orig_report_power {*}$args]
}

rename ::orfs_write_db ::orfs_async_orig_write_db
proc ::orfs_write_db { output_file { endpoint_slack 1 } } {
  ::orfs_async_orig_write_db $output_file $endpoint_slack
  if { [llength $::orfs_async_pending] == 0 || [string match "*-failed*" $output_file] } {
    return
  }
  set odb [file tail $output_file]
  foreach p $::orfs_async_pending {
    lassign $p name stage when erc skew src prewarm
    ::orfs_async_spawn $name report $odb $src [::orfs_async_metrics_stage 0] $prewarm $stage $when $erc $skew
  }
  set ::orfs_async_pending {}
}

rename ::set_pdnsim_net_voltage ::orfs_async_orig_set_pdnsim_net_voltage
proc ::set_pdnsim_net_voltage { args } {
  set i [lsearch -exact $args -net]
  set j [lsearch -exact $args -voltage]
  if { $i >= 0 && $j >= 0 } {
    set ::orfs_async_volts([lindex $args [expr { $i + 1 }]]) [lindex $args [expr { $j + 1 }]]
  }
  uplevel 1 [list ::orfs_async_orig_set_pdnsim_net_voltage {*}$args]
}

rename ::analyze_power_grid ::orfs_async_orig_analyze_power_grid
proc ::analyze_power_grid { args } {
  set prewarm $::orfs_async_power_used
  set ::orfs_async_power_used 1
  if { ![info exists ::env(ORFS_ASYNC_FINISH_PARALLEL)] || $::env(ORFS_ASYNC_FINISH_PARALLEL) ne "1" } {
    return [uplevel 1 [list ::orfs_async_orig_analyze_power_grid {*}$args]]
  }
  set i [lsearch -exact $args -net]
  set net [lindex $args [expr { $i + 1 }]]
  set name [::orfs_async_name "irdrop $net"]
  ::orfs_async_slot $name
  # the job sets every voltage set so far in this process, as the inline call had them
  ::orfs_async_spawn $name irdrop 6_final.odb [est::parasitics_source] [::orfs_async_metrics_stage 0] $prewarm \
    [array get ::orfs_async_volts] {*}$args
}
