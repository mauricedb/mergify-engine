$(function() {
  // WHY?
  $("body").fadeIn(0)

  // The default href for the current navbar on load is # which does not match the first h1 title and breaks scrollspy
  $("li.toctree-l1.current > a").attr("href", "#" + $("span#id1").parent().attr('id'));

  $('body').scrollspy({ target: 'div.sphinxsidebar', offset: 48 })

  // Change class from current to active for navball pills
  $("div.sphinxsidebar a.reference.current").removeClass("current").addClass("active")

  // Grid layout Style
  $(".sphinxsidebar > ul").addClass('nav flex-column nav-pills')
    .find('li').addClass('nav-item').end()
    .find('a.reference').addClass('nav-link').end()

  $(".related").addClass("col-md-12");
  $(".footer").addClass("col-md-12");

  // Tables
  $("table.docutils").addClass("table table-sm table-bordered table-striped")
    .find("thead")
    .addClass("thead-dark")

  // Admonition
  $(".admonition").addClass("alert").removeClass("admonition")
    .filter(".hint").removeClass("hint").addClass("alert-info").children('p.admonition-title').prepend('<div class="icon"></div>').end().end()
    .filter(".note").removeClass("note").addClass("alert-primary").children('p.admonition-title').prepend('<div class="icon"></div>').end().end()
    .filter(".warning").removeClass("warning").addClass("alert-warning").children('p.admonition-title').prepend('<div class="icon"></div>').end().end()

  // images
  $(".documentwrapper img").addClass("img-fluid");

});


// Scroll to the anchor
// why is this needed? lol chrome? :(
$(document).ready(function () {
  var isChrome = /Chrome/.test(navigator.userAgent) && /Google Inc/.test(navigator.vendor);
  if (window.location.hash && isChrome) {
    setTimeout(function () {
      var hash = window.location.hash;
      window.location.hash = "";
      window.location.hash = hash;
    }, 300);
  }
});
