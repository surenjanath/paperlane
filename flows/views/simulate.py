"""Workflow simulator page: start a background run, poll progress, show routes and problems."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render

from .. import simulator
from ..permissions import get_workflow


@login_required
def simulate(request, pk):
    wf = get_workflow(request, pk, edit=True)
    if request.method == "POST":
        if not simulator.start(wf, request.user):
            messages.info(request, "A simulation is already running for this workflow.")
        return redirect("flows:simulate", wf.pk)
    st = simulator.status(wf.pk)
    ctx = {"wf": wf, "st": st, "nav": "workflows"}
    if st.get("status") == "done":
        r = st["result"]
        ctx.update(result=r, routes=simulator.routes(r), problems=sorted(r["problems"], key=lambda p: ["high", "medium", "low"].index(p["severity"])))
    return render(request, "flows/simulate.html", ctx)


@login_required
def simulate_status(request, pk):
    wf = get_workflow(request, pk, edit=True)
    st = simulator.status(wf.pk)
    return JsonResponse({k: v for k, v in st.items() if k != "result"})
